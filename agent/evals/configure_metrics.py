#!/usr/bin/env python3
"""
Post-push eval configuration: allow "extra" tool calls in golden scoring.

Our golden YAML (goldens.yaml) intentionally omits reference tool-traces, because CES
golden `toolCall` expectations can only reference top-level tools (`apps/<app>/tools/<id>`)
— they cannot express **OpenAPI-toolset** operations (`.../toolsets/servicing/tools/...`).
Without this, the platform's auto tool-trajectory metric flags every real tool call as an
"extra" (unexpected) call and fails the test case, even though the behaviour is correct.

This sets each evaluation's threshold override to `extra_tool_call_behavior = ALLOW`, so the
agent's real tool calls are permitted and the **behavioural LLM expectations** decide
pass/fail. Idempotent — safe to re-run after every `cxas push-eval`.

    .venv-scrapi/bin/python agent/evals/configure_metrics.py

Then re-run:  cxas run --app-name <APP> --tags account policy hardship memory compliance --wait
"""
import os

from cxas_scrapi.utils.eval_utils import EvalUtils
from google.cloud.ces_v1beta.types import app as appt

APP = os.environ.get(
    "SCRAPI_APP",
    "projects/gcex-pilot-16862/locations/us/apps/tilicho-credit-scrapi",
)


def main():
    eu = EvalUtils(APP)
    thr = appt.EvaluationMetricsThresholds(
        golden_evaluation_metrics_thresholds=appt.EvaluationMetricsThresholds.GoldenEvaluationMetricsThresholds(
            tool_matching_settings=appt.EvaluationMetricsThresholds.ToolMatchingSettings(
                extra_tool_call_behavior=2  # ALLOW (0=UNSPECIFIED, 1=FAIL, 2=ALLOW)
            )
        )
    )
    evals = eu.list_evaluations()
    for ev in evals:
        ev.evaluation_metrics_threshold_override = thr
        eu.update_evaluation(ev)
        print("allow-extra-tools set:", ev.display_name)
    print(f"Done — {len(evals)} evaluation(s) configured.")


if __name__ == "__main__":
    main()
