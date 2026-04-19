import datetime
import json
import os
import re
import sys
import textwrap
import threading
import time
import zoneinfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Per-task thread-local context: task_id + log_fh per worker thread
_task_local = threading.local()

# Run-level state set by _setup_log_tee(), used by _run_single_task and main()
_run_dir: "Path | None" = None   # logs/{ts}_{model}/ directory for this run


# ---------------------------------------------------------------------------
# FIX-110: LOG_LEVEL env + auto-tee stdout → logs/{ts}_{model}.log
# Must be set up before agent/dispatch imports (they print at import time).
# ---------------------------------------------------------------------------

def _setup_log_tee() -> None:
    """Tee stdout to logs/{ts}_{model}.log. ANSI codes are stripped in file."""
    # Read MODEL_DEFAULT and LOG_LEVEL from env or .env file (no import side-effects yet)
    _env_path = Path(__file__).parent / ".env"
    _dotenv: dict[str, str] = {}
    try:
        for _line in _env_path.read_text().splitlines():
            _s = _line.strip()
            if _s and not _s.startswith("#") and "=" in _s:
                _k, _, _v = _s.partition("=")
                _dotenv[_k.strip()] = _v.strip()
    except Exception:
        pass

    model = os.getenv("MODEL_DEFAULT") or _dotenv.get("MODEL_DEFAULT") or "unknown"
    log_level = (os.getenv("LOG_LEVEL") or _dotenv.get("LOG_LEVEL") or "INFO").upper()

    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(exist_ok=True)

    _tz_name = os.environ.get("TZ", "")
    try:
        _tz = zoneinfo.ZoneInfo(_tz_name) if _tz_name else None
    except Exception:
        _tz = None
    _now = datetime.datetime.now(tz=_tz) if _tz else datetime.datetime.now()
    _safe = model.replace("/", "-").replace(":", "-")
    run_name = f"{_now.strftime('%Y%m%d_%H%M%S')}_{_safe}"
    run_path = logs_dir / run_name
    run_path.mkdir(exist_ok=True)
    global _run_dir
    _run_dir = run_path

    _fh = open(run_path / "main.log", "w", buffering=1, encoding="utf-8")
    _ansi = re.compile(r"\x1B\[[0-9;]*[A-Za-z]")
    _orig = sys.stdout

    class _Tee:
        def write(self, data: str) -> None:
            prefix = getattr(_task_local, "task_id", None)
            task_fh = getattr(_task_local, "log_fh", None)
            clean = _ansi.sub("", data)
            # Terminal: prefix for parallel tasks
            if prefix and data and data != "\n":
                _orig.write(f"[{prefix}] {data}")
            else:
                _orig.write(data)
            # Per-task log or main.log
            if task_fh is not None:
                task_fh.write(clean)
            else:
                _fh.write(clean)

        def flush(self) -> None:
            _orig.flush()
            _fh.flush()
            task_fh = getattr(_task_local, "log_fh", None)
            if task_fh is not None:
                task_fh.flush()

        def isatty(self) -> bool:
            return _orig.isatty()

        @property
        def encoding(self) -> str:
            return _orig.encoding

    sys.stdout = _Tee()
    print(f"[LOG] {run_path}/  (LOG_LEVEL={log_level})")


LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()  # re-exported for external use
_setup_log_tee()

from agent.tracer import init_tracer as _init_tracer, close_tracer as _close_tracer, set_task_id as _set_task_id
if _run_dir is not None:
    _init_tracer(str(_run_dir))


from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest, EvalPolicy, GetBenchmarkRequest,
    StartRunRequest, StartTrialRequest,
    StatusRequest, SubmitRunRequest,
)
from connectrpc.errors import ConnectError

from agent import run_agent
from agent.classifier import ModelRouter
from agent.dspy_examples import record_example as _record_dspy_example
from agent.dspy_examples import record_eval_example as _record_eval_example

_DSPY_COLLECT = os.getenv("DSPY_COLLECT", "1") == "1"

BITGN_URL = os.getenv("BENCHMARK_HOST") or "https://api.bitgn.com"
BENCHMARK_ID = os.getenv("BENCHMARK_ID") or "bitgn/pac1-dev"
BITGN_API_KEY = os.getenv("BITGN_API_KEY") or ""
_base_run_name = os.getenv("BITGN_RUN_NAME") or ""
BITGN_RUN_NAME = f"{_base_run_name}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}" if _base_run_name else ""
PARALLEL_TASKS = max(1, int(os.getenv("PARALLEL_TASKS", "1")))

_MODELS_JSON = Path(__file__).parent / "models.json"
_raw = json.loads(_MODELS_JSON.read_text())
_profiles: dict[str, dict] = _raw.get("_profiles", {})  # FIX-119: named parameter profiles
MODEL_CONFIGS: dict[str, dict] = {k: v for k, v in _raw.items() if not k.startswith("_")}
# FIX-119: resolve profile name references in ollama_options fields (string → dict)
for _cfg in MODEL_CONFIGS.values():
    for _fname in ("ollama_options", "ollama_options_classifier",
                   "ollama_options_evaluator", "ollama_options_queue", "ollama_options_capture",
                   "ollama_options_crm", "ollama_options_temporal"):
        if isinstance(_cfg.get(_fname), str):
            _cfg[_fname] = _profiles.get(_cfg[_fname], {})

# FIX-91: все типы задаются явно — MODEL_ID как fallback упразднён.
# Каждая переменная обязательна; если не задана — ValueError при старте.
def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise ValueError(f"Env var {name} is required but not set. Check .env or environment.")
    return v

_model_classifier = _require_env("MODEL_CLASSIFIER")
_model_default    = _require_env("MODEL_DEFAULT")

# Optional per-type overrides — fall back to default if not set
_model_email    = os.getenv("MODEL_EMAIL")    or _model_default
_model_lookup   = os.getenv("MODEL_LOOKUP")   or _model_default
_model_inbox    = os.getenv("MODEL_INBOX")    or _model_default
_model_queue    = os.getenv("MODEL_QUEUE")    or _model_inbox
_model_capture  = os.getenv("MODEL_CAPTURE")  or _model_default
_model_crm      = os.getenv("MODEL_CRM")      or _model_default
_model_temporal = os.getenv("MODEL_TEMPORAL") or _model_lookup
_model_preject  = os.getenv("MODEL_PREJECT")  or _model_default
_model_evaluator      = os.getenv("MODEL_EVALUATOR")      or _model_default
_model_prompt_builder = os.getenv("MODEL_PROMPT_BUILDER") or ""  # "" = use classifier
_model_codegen        = os.getenv("MODEL_CODEGEN")        or ""  # "" = use task-type model

EFFECTIVE_MODEL: ModelRouter = ModelRouter(
    default=_model_default,
    classifier=_model_classifier,
    email=_model_email,
    lookup=_model_lookup,
    inbox=_model_inbox,
    queue=_model_queue,
    capture=_model_capture,
    crm=_model_crm,
    temporal=_model_temporal,
    preject=_model_preject,
    codegen=_model_codegen,
    evaluator=_model_evaluator,
    prompt_builder=_model_prompt_builder,
    configs=MODEL_CONFIGS,
)
print(
    f"[MODEL_ROUTER] Multi-model mode:\n"
    f"  classifier  = {_model_classifier}\n"
    f"  default     = {_model_default}\n"
    f"  email       = {_model_email}\n"
    f"  lookup      = {_model_lookup}\n"
    f"  inbox       = {_model_inbox}\n"
    f"  queue       = {_model_queue}\n"
    f"  capture     = {_model_capture}\n"
    f"  crm         = {_model_crm}\n"
    f"  temporal    = {_model_temporal}\n"
    f"  preject     = {_model_preject}\n"
    f"  codegen     = {_model_codegen or '(uses task-type model)'}\n"
    f"  evaluator   = {_model_evaluator}\n"
    f"  builder     = {_model_prompt_builder or '(uses classifier)'}"
)

def _print_run_params() -> None:
    """Print structured run parameters to main.log for cross-run comparison."""
    _g = os.getenv
    sep = "─" * 56

    _base = Path(__file__).parent
    builder_prog = _base / "data" / "prompt_builder_program.json"
    eval_prog    = _base / "data" / "evaluator_program.json"
    builder_status = "[loaded]" if builder_prog.exists() else "[missing]"
    eval_status    = "[loaded]" if eval_prog.exists() else "[missing]"

    cli_tasks  = " ".join(sys.argv[1:]) or "(all)"
    tz_val     = _g("TZ", "") or "(system)"
    eval_on    = _g("EVALUATOR_ENABLED", "1") == "1"
    pb_on      = _g("PROMPT_BUILDER_ENABLED", "1") == "1"

    print(
        f"\n[RUN_PARAMS] {'═' * 56}\n"
        f"  cli_tasks        = {cli_tasks}\n"
        f"  benchmark_id     = {BENCHMARK_ID}\n"
        f"  benchmark_host   = {BITGN_URL}\n"
        f"  run_name         = {_base_run_name or '(not set)'}\n"
        f"  parallel_tasks   = {_g('PARALLEL_TASKS', '1')}\n"
        f"  task_timeout_s   = {_g('TASK_TIMEOUT_S', '300')}\n"
        f"  log_level        = {_g('LOG_LEVEL', 'INFO')}\n"
        f"  tz               = {tz_val}\n"
        f"  {sep}\n"
        f"  router_fallback  = {_g('ROUTER_FALLBACK', 'CLARIFY')}\n"
        f"  router_retries   = {_g('ROUTER_MAX_RETRIES', '2')}\n"
        f"  {sep}\n"
        f"  evaluator        = {'on' if eval_on else 'off'}"
        f" | skepticism={_g('EVAL_SKEPTICISM', 'mid')}"
        f" | efficiency={_g('EVAL_EFFICIENCY', 'mid')}"
        f" | max_rejections={_g('EVAL_MAX_REJECTIONS', '2')}\n"
        f"  eval_program     = {eval_status}\n"
        f"  {sep}\n"
        f"  prompt_builder   = {'on' if pb_on else 'off'}"
        f" | max_tokens={_g('PROMPT_BUILDER_MAX_TOKENS', '500')}\n"
        f"  builder_program  = {builder_status}\n"
        f"  dspy_collect     = {_g('DSPY_COLLECT', '1')}\n"
        f"  {sep}\n"
        f"  python           = {sys.version.split()[0]}\n"
        f"[RUN_PARAMS] {'═' * 56}"
    )


_print_run_params()

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"


def _run_single_task(trial_id: str, task_filter: list, router: ModelRouter) -> tuple:
    """Execute one benchmark trial in its own thread with a dedicated harness client."""
    client = HarnessServiceClientSync(BITGN_URL)
    trial = client.start_trial(StartTrialRequest(trial_id=trial_id))
    task_id = trial.task_id

    if task_filter and task_id not in task_filter:
        # Skip filtered-out tasks — do not end trial; submit_run(force=True) handles cleanup
        return (task_id, -1, [], 0.0, {})

    _task_local.task_id = task_id  # stdout prefix for this thread
    _set_task_id(task_id)          # tracer thread-local (TRACE_ENABLED=1 only, no-op otherwise)
    assert _run_dir is not None, "_run_dir not initialised by _setup_log_tee"
    _task_local.log_fh = open(_run_dir / f"{task_id}.log", "w", buffering=1, encoding="utf-8")
    try:
        task_start = time.time()
        print(f"\n{'=' * 30} Starting task: {task_id} {'=' * 30}")
        print(f"{CLI_BLUE}{trial.instruction}{CLI_CLR}\n{'-' * 80}")
        token_stats: dict = {"input_tokens": 0, "output_tokens": 0}
        try:
            token_stats = run_agent(router, trial.harness_url, trial.instruction)
        except Exception as exc:
            print(exc)
        task_elapsed = time.time() - task_start
        result = client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
        score = result.score
        detail = list(result.score_detail)
        # Variant 4: record examples for DSPy COPRO optimisation (DSPY_COLLECT=1)
        _score_f = float(score)
        if _DSPY_COLLECT:
            if token_stats.get("builder_used") and token_stats.get("builder_addendum"):
                _record_dspy_example(
                    task_text=trial.instruction,
                    task_type=token_stats.get("task_type", "default"),
                    addendum=token_stats["builder_addendum"],
                    score=_score_f,
                    vault_tree=token_stats.get("builder_vault_tree", ""),
                    agents_md=token_stats.get("builder_agents_md", ""),
                )
            _eval_call = token_stats.get("eval_last_call")
            if _eval_call and token_stats.get("evaluator_calls", 0) > 0:
                _record_eval_example(**_eval_call, score=_score_f)
        style = CLI_GREEN if score == 1 else CLI_RED
        in_t   = token_stats.get("input_tokens", 0)
        out_t  = token_stats.get("output_tokens", 0)
        steps  = token_stats.get("step_count", 0)
        calls  = token_stats.get("llm_call_count", 0)
        t_type = token_stats.get("task_type", "—")
        m_short = (token_stats.get("model_used") or "—").split("/")[-1]
        detail_str = "\n" + textwrap.indent("\n".join(detail), "  ") if detail else ""
        print(
            f"{style}[{task_id}] Score: {score:0.2f}"
            f" | {task_elapsed:.1f}s"
            f" | {steps}st {calls}rq"
            f" | in {in_t:,} / out {out_t:,} tok"
            f" | {t_type} | {m_short}"
            f"{detail_str}{CLI_CLR}"
        )
        return (task_id, score, detail, task_elapsed, token_stats)
    finally:
        fh = _task_local.log_fh
        _task_local.log_fh = None
        if fh:
            fh.flush()
            fh.close()


_TABLE_W = 196
_TABLE_SEP = "=" * _TABLE_W


def _print_table_header() -> None:
    """Print the summary table header to main.log (call once before tasks start)."""
    print(f"\n{_TABLE_SEP}")
    print(f"{'ИТОГОВАЯ СТАТИСТИКА':^{_TABLE_W}}")
    print(_TABLE_SEP)
    print(f"{'Задание':<10} {'Оценка':>7} {'Время':>8}  {'Шаги':>5} {'Запр':>5} {'Eval':>4} {'EvMs':>6}  {'Вход(tok)':>10} {'Выход(tok)':>10} {'ток/с':>7}  {'B':>1} {'BIn':>6} {'BOt':>5}  {'Тип':<11} {'Модель':<34}  Проблемы")
    print("-" * _TABLE_W)


def _print_table_row(task_id: str, score: float, detail: list, elapsed: float, ts: dict) -> None:
    """Print one completed-task row into the summary table."""
    issues = "; ".join(detail) if score < 1.0 else "—"
    in_t   = ts.get("input_tokens", 0)
    out_t  = ts.get("output_tokens", 0)
    llm_ms = ts.get("llm_elapsed_ms", 0)
    ev_c   = ts.get("ollama_eval_count", 0)
    ev_ms  = ts.get("ollama_eval_ms", 0)
    steps  = ts.get("step_count", 0)
    calls  = ts.get("llm_call_count", 0)
    eval_c = ts.get("evaluator_calls", 0)
    eval_ms = ts.get("evaluator_ms", 0)
    if ev_c > 0 and ev_ms > 0:
        tps = ev_c / (ev_ms / 1000.0)
    elif llm_ms > 0:
        tps = out_t / (llm_ms / 1000.0)
    else:
        tps = 0.0
    m = ts.get("model_used", "—")
    m_short = m.split("/")[-1] if "/" in m else m
    t_type = ts.get("task_type", "—")
    b_flag = "✓" if ts.get("builder_used") else "—"
    b_in   = ts.get("builder_in_tok", 0)
    b_out  = ts.get("builder_out_tok", 0)
    print(f"{task_id:<10} {score:>7.2f} {elapsed:>7.1f}s  {steps:>5} {calls:>5} {eval_c:>4} {eval_ms:>6}  {in_t:>10,} {out_t:>10,} {tps:>6.0f}  {b_flag:>1} {b_in:>6,} {b_out:>5,}  {t_type:<11} {m_short:<34}  {issues}")


def _write_summary(scores: list, run_start: float) -> None:
    """Print totals + per-model breakdown after all rows have been printed."""
    n = len(scores)
    total = sum(s for _, s, *_ in scores) / n * 100.0
    total_elapsed = time.time() - run_start
    total_in = total_out = total_llm_ms = total_steps = total_calls = 0
    total_eval_calls = total_eval_ms_sum = 0
    total_b_used = total_b_in = total_b_out = 0
    model_totals: dict[str, dict] = {}
    for task_id, score, detail, elapsed, ts in scores:
        in_t   = ts.get("input_tokens", 0)
        out_t  = ts.get("output_tokens", 0)
        llm_ms = ts.get("llm_elapsed_ms", 0)
        ev_c   = ts.get("ollama_eval_count", 0)
        ev_ms  = ts.get("ollama_eval_ms", 0)
        total_in  += in_t;  total_out += out_t
        total_llm_ms += llm_ms
        total_steps += ts.get("step_count", 0)
        total_calls += ts.get("llm_call_count", 0)
        total_eval_calls  += ts.get("evaluator_calls", 0)
        total_eval_ms_sum += ts.get("evaluator_ms", 0)
        total_b_used += 1 if ts.get("builder_used") else 0
        total_b_in   += ts.get("builder_in_tok", 0)
        total_b_out  += ts.get("builder_out_tok", 0)
        m = ts.get("model_used", "—")
        if m not in model_totals:
            model_totals[m] = {"in": 0, "out": 0, "llm_ms": 0, "ev_c": 0, "ev_ms": 0, "elapsed": 0, "count": 0}
        mt = model_totals[m]
        mt["in"] += in_t;  mt["out"] += out_t
        mt["llm_ms"] += llm_ms;  mt["ev_c"] += ev_c;  mt["ev_ms"] += ev_ms
        mt["elapsed"] += elapsed;  mt["count"] += 1
    total_tasks_elapsed = sum(e for _, _, _, e, _ in scores)
    avg_elapsed = total_tasks_elapsed / n
    avg_in  = total_in  // n
    avg_out = total_out // n
    avg_steps = total_steps // n
    avg_calls = total_calls // n
    avg_eval_c  = total_eval_calls  // n
    avg_eval_ms = total_eval_ms_sum // n
    total_ev_c  = sum(ts.get("ollama_eval_count", 0) for *_, ts in scores)
    total_ev_ms = sum(ts.get("ollama_eval_ms",    0) for *_, ts in scores)
    if total_ev_c > 0 and total_ev_ms > 0:
        total_tps = total_ev_c / (total_ev_ms / 1000.0)
    elif total_llm_ms > 0:
        total_tps = total_out / (total_llm_ms / 1000.0)
    else:
        total_tps = 0.0
    print(_TABLE_SEP)
    print(f"{'ИТОГО':<10} {total:>6.2f}% {total_elapsed:>7.1f}s  {total_steps:>5} {total_calls:>5} {total_eval_calls:>4} {total_eval_ms_sum:>6}  {total_in:>10,} {total_out:>10,} {total_tps:>6.0f}  {total_b_used:>1} {total_b_in:>6,} {total_b_out:>5,}  {'':11} {'':34}")
    print(f"{'СРЕДНЕЕ':<10} {'':>7} {avg_elapsed:>7.1f}s  {avg_steps:>5} {avg_calls:>5} {avg_eval_c:>4} {avg_eval_ms:>6}  {avg_in:>10,} {avg_out:>10,} {'':>6}  {'':>1} {'':>6} {'':>5}  {'':11} {'':34}")
    print(_TABLE_SEP)
    if len(model_totals) > 1:
        print(f"\n{'─' * 84}")
        print("По моделям:")
        print(f"{'─' * 84}")
        print(f"  {'Модель':<35} {'Задач':>5}  {'Вх.всего':>10}  {'Вх.ср.':>10}  {'Вых.ср.':>9}  {'с/задачу':>9}  {'ток/с':>7}")
        print(f"  {'─' * 82}")
        for m, mt in sorted(model_totals.items()):
            m_short = m.split("/")[-1] if "/" in m else m
            cnt = mt["count"]
            avg_i = mt["in"]  // cnt if cnt else 0
            avg_o = mt["out"] // cnt if cnt else 0
            avg_e = mt["elapsed"] / cnt if cnt else 0.0
            m_ev_c   = mt["ev_c"];   m_ev_ms  = mt["ev_ms"]
            m_llm_ms = mt["llm_ms"]
            if m_ev_c > 0 and m_ev_ms > 0:
                m_tps = m_ev_c / (m_ev_ms / 1000.0)
            elif m_llm_ms > 0:
                m_tps = mt["out"] / (m_llm_ms / 1000.0)
            else:
                m_tps = 0.0
            print(f"  {m_short:<35} {cnt:>5}  {mt['in']:>10,}  {avg_i:>10,}  {avg_o:>9,}  {avg_e:>8.1f}s  {m_tps:>6.0f}")


def main() -> None:
    task_filter = sys.argv[1:]

    scores = []
    scores_lock = threading.Lock()
    run_start = time.time()
    try:
        client = HarnessServiceClientSync(BITGN_URL)
        print("Connecting to BitGN", client.status(StatusRequest()))
        res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCHMARK_ID))
        print(
            f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
            f"with {len(res.tasks)} tasks.\n{CLI_GREEN}{res.description}{CLI_CLR}"
        )

        run = client.start_run(StartRunRequest(
            name=BITGN_RUN_NAME,
            benchmark_id=BENCHMARK_ID,
            api_key=BITGN_API_KEY,
        ))
        print(f"Run started: {run.run_id} ({len(run.trial_ids)} trials)")

        try:
            _print_table_header()
            with ThreadPoolExecutor(max_workers=PARALLEL_TASKS) as pool:
                futures = {
                    pool.submit(_run_single_task, tid, task_filter, EFFECTIVE_MODEL): tid
                    for tid in run.trial_ids
                }
                for fut in as_completed(futures):
                    try:
                        task_id, score, detail, task_elapsed, token_stats = fut.result()
                    except Exception as exc:
                        failed_tid = futures[fut]
                        print(f"{CLI_RED}[{failed_tid}] Task error: {exc}{CLI_CLR}")
                        continue
                    if score >= 0:
                        with scores_lock:
                            scores.append((task_id, score, detail, task_elapsed, token_stats))
                        _print_table_row(task_id, score, detail, task_elapsed, token_stats)
        finally:
            client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))
            print(f"Run submitted: {run.run_id}")

    except ConnectError as exc:
        print(f"{exc.code}: {exc.message}")
    except KeyboardInterrupt:
        print(f"{CLI_RED}Interrupted{CLI_CLR}")

    if scores:
        _write_summary(scores, run_start)


if __name__ == "__main__":
    try:
        main()
    finally:
        _close_tracer()
