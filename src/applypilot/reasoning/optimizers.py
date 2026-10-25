"""Existing learning entry points using public DSPy optimizer APIs."""
from __future__ import annotations

CHALLENGER_TASKS = frozenset({"browser_observation", "application_form"})


def select_optimizer(task, previous, *, enabled=True):
    """Alternate completed rounds against the current champion; retry failures."""
    if not enabled or task not in CHALLENGER_TASKS:
        return "gepa"
    name = previous.get("optimizer", "gepa")
    if previous.get("state") in {"failed", "interrupted"}:
        return name if name in {"gepa", "mipro"} else "gepa"
    if previous.get("state") in {"promoted", "candidate", "rejected", "unchanged"}:
        return "mipro" if name == "gepa" else "gepa"
    return "gepa"


def compile_mipro(student, examples, evaluator, reflection, cfg, report):
    """DSPy owns candidate generation, evaluation and Bayesian optimization.

    No subclass overrides or private Optuna loop. Parameters below describe the
    optimizer algorithm, not worker admission or application retry scheduling.
    Transport failures propagate through DSPy's public max_errors setting.
    """
    from .dspy_runtime import dspy, ProposalAdapter

    train, validation = examples["train"], examples["validation"]
    if not train or not validation:
        raise ValueError("MIPROv2 requires nonempty training and validation sets")
    candidates = int(cfg.get("mipro_candidates", 4))
    trials = int(cfg.get("mipro_trials", 10))
    if candidates < 2 or trials < 1:
        raise ValueError("MIPROv2 requires at least two candidates and one trial")

    def metric(gold, prediction, trace=None):
        return evaluator.assess(gold, prediction)["score"]

    optimizer = dspy.MIPROv2(metric=metric, prompt_model=reflection,
        task_model=reflection, max_bootstrapped_demos=0, max_labeled_demos=0,
        auto=None, num_candidates=candidates, num_threads=1, max_errors=1,
        seed=0, track_stats=True, log_dir=None)
    with dspy.context(adapter=ProposalAdapter(), disable_history=True, lm=reflection):
        candidate = optimizer.compile(student, trainset=train, valset=validation,
            num_trials=trials, minibatch=False, program_aware_proposer=False,
            data_aware_proposer=True, view_data_batch_size=len(train),
            fewshot_aware_proposer=False, tip_aware_proposer=False)
    report["optimizer_api"] = "dspy.MIPROv2.compile"
    report["optimizer_trials"] = trials
    # DSPy's score is diagnostic; independent holdout/promotion checks remain
    # the responsibility of the learning domain, never this library adapter.
    score = getattr(candidate, "score", None)
    if isinstance(score, (int, float)):
        report["selected_validation_score"] = score
    return candidate
