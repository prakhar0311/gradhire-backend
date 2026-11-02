import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from app.services.ai_optimizer import get_openai_client


# =====================================================
# AI CHECK FOR UNCLEAR JOBS
# =====================================================
# Jobs the rules (fresher.py) can't decide are classified by a small model
# at temperature 0, ten per call. Each job is classified once: results are
# kept in memory and in public.jobs, and loaded from there first.
#
# A request waits at most WAIT_SECONDS. Anything still running finishes in
# the background and is used from the next request on; until then the job
# is shown (an unclear job is never hidden just because the check is slow).

CLASSIFICATION_VERSION = 1
MODEL = "gpt-4o-mini"
BATCH_SIZE = 10
WAIT_SECONDS = 6.0
MAX_CONCURRENT_CALLS = 4
DESCRIPTION_CHARS = 600

# A "not for freshers" answer only hides a job when the model is this sure
MIN_CONFIDENCE_TO_EXCLUDE = 0.6

# Hard cap (~$1/day at gpt-4o-mini prices); beyond it jobs stay unclear
DAILY_JOB_LIMIT = 20000

MEMORY_MAX = 20000

PROMPT = """You review job postings for a job board for freshers: people with 0-1 years of work experience (new graduates, final-year students).
For each job you get the title, company and the start of the description (it may be cut off). Judge only from that text.

fresher = true when someone with 0-1 years of experience could realistically be hired: trainee, intern, graduate and junior roles, roles that say freshers are welcome, roles asking for at most 1 year, or roles with no stated requirement and no sign of seniority.
fresher = false when the role needs more than 1 year of experience, or reads as senior or experienced: "seasoned", "experienced developer", "proven track record", "strong background in", SDE II or higher, lead/architect duties, owning system design, mentoring others, a mid-level grade, or a program for people returning to work after a career break.
min_years = the minimum years of experience the text states, else null (don't guess).
batches = graduation years the posting accepts (e.g. [2025, 2026]) if stated, else [].
confidence = 0 to 1, how sure you are given only this excerpt.

Return JSON only: {"jobs": [{"i": <index>, "fresher": true|false, "min_years": number|null, "batches": [int], "confidence": number}]} with one entry per job."""


_memory: "OrderedDict[str, dict]" = OrderedDict()
_in_flight: set = set()
_lock = threading.Lock()
# Its own small pool, so model calls never queue other thread-pool work
_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CALLS, thread_name_prefix="fresher-ai")
_background: set = set()
_day = {"date": "", "count": 0}


def _remember(job_id: str, result: dict):
    with _lock:
        _memory[job_id] = result
        _memory.move_to_end(job_id)
        while len(_memory) > MEMORY_MAX:
            _memory.popitem(last=False)


def _known(job_id: str) -> Optional[dict]:
    with _lock:
        return _memory.get(job_id)


def _take_budget(count: int) -> int:
    """How many of `count` jobs may still be classified today."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with _lock:
        if _day["date"] != today:
            _day.update(date=today, count=0)
        allowed = max(0, min(count, DAILY_JOB_LIMIT - _day["count"]))
        _day["count"] += allowed
        return allowed


# -----------------------------------------------------
# One model call
# -----------------------------------------------------

def parse_response(content: str, count: int) -> Dict[int, dict]:
    """Valid entries by index. Anything malformed is dropped (that job just
    stays unclear)."""

    try:
        parsed = json.loads(content or "")
    except ValueError:
        return {}
    entries = parsed.get("jobs") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        return {}

    results: Dict[int, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index, fresher, confidence = entry.get("i"), entry.get("fresher"), entry.get("confidence")
        if not isinstance(index, int) or not 0 <= index < count or not isinstance(fresher, bool):
            continue
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            continue
        min_years = entry.get("min_years")
        if not isinstance(min_years, (int, float)) or isinstance(min_years, bool) or not 0 <= min_years <= 15:
            min_years = None
        batches = entry.get("batches") if isinstance(entry.get("batches"), list) else []
        batches = sorted({b for b in batches if isinstance(b, int) and 2020 <= b <= 2035})
        results[index] = {
            "fresher": fresher,
            "min_years": float(min_years) if min_years is not None else None,
            "batches": batches,
            "confidence": round(float(confidence), 2),
        }
    return results


def _describe(index: int, job: dict) -> str:
    description = " ".join((job.get("description") or "").split())[:DESCRIPTION_CHARS]
    return f"[{index}] Title: {job.get('title') or ''}\nCompany: {job.get('company') or ''}\nDescription: {description}"


def _classify_batch(jobs: List[dict]) -> None:
    """Runs in a worker thread: one call, then memory and Supabase."""

    user = "\n\n".join(_describe(i, job) for i, job in enumerate(jobs))
    results: Dict[int, dict] = {}

    try:
        for _ in range(2):
            try:
                response = get_openai_client().chat.completions.create(
                    model=MODEL,
                    temperature=0,
                    response_format={"type": "json_object"},
                    max_tokens=60 * len(jobs) + 50,
                    messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": user}],
                )
                results = parse_response(response.choices[0].message.content, len(jobs))
                if results:
                    break
            except Exception as e:
                logging.warning(f"Fresher classification failed: {type(e).__name__}")

        for index, result in results.items():
            _remember(jobs[index]["id"], result)
        if results:
            from app.services.job_store import save_classifications  # job_store imports jobs, which imports this
            save_classifications([(jobs[i], r) for i, r in results.items()], CLASSIFICATION_VERSION)
    finally:
        with _lock:
            for job in jobs:
                _in_flight.discard(job["id"])


async def _run_batch(jobs: List[dict]):
    await asyncio.get_running_loop().run_in_executor(_executor, _classify_batch, jobs)


# -----------------------------------------------------
# Public
# -----------------------------------------------------

async def classify(jobs: List[dict], wait: float = WAIT_SECONDS) -> Dict[str, dict]:
    """Classifications for these jobs (by id) that are known within `wait`
    seconds. Unknown jobs are looked up in Supabase, then sent to the model."""

    wanted = {job["id"]: job for job in jobs if job.get("id")}
    missing = [job_id for job_id in wanted if _known(job_id) is None]

    if missing:
        from app.services.job_store import load_classifications  # see _classify_batch
        stored = await asyncio.to_thread(load_classifications, missing, CLASSIFICATION_VERSION)
        for job_id, result in stored.items():
            _remember(job_id, result)

    with _lock:
        todo = [wanted[i] for i in missing if i not in _memory and i not in _in_flight]
        _in_flight.update(job["id"] for job in todo)

    allowed = _take_budget(len(todo))
    if allowed < len(todo):
        with _lock:
            for job in todo[allowed:]:
                _in_flight.discard(job["id"])
        if allowed == 0 and todo:
            logging.warning("Fresher classification daily limit reached")
        todo = todo[:allowed]

    tasks = []
    for start in range(0, len(todo), BATCH_SIZE):
        task = asyncio.ensure_future(_run_batch(todo[start:start + BATCH_SIZE]))
        # Keep a reference: tasks still running after `wait` finish in the background
        _background.add(task)
        task.add_done_callback(_background.discard)
        tasks.append(task)

    if tasks and wait > 0:
        await asyncio.wait(tasks, timeout=wait)

    return {job_id: result for job_id in wanted if (result := _known(job_id)) is not None}


def excludes(result: Optional[dict]) -> bool:
    return bool(result) and not result["fresher"] and result["confidence"] >= MIN_CONFIDENCE_TO_EXCLUDE
