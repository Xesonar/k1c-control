Obico temporal scoring — source and integration notes

scoring.py is a standalone port of the mathematical parts of Obico's warning
judge, without Django models, Redis, database migrations, camera requests or
physical actions. It does not replace the bot's AlarmController.

Source attribution: TheSpaghettiDetective/obico-server contributors, commit
df8190ef7500459599ebf398c3d1f653754792fd:
https://github.com/TheSpaghettiDetective/obico-server/blob/df8190ef7500459599ebf398c3d1f653754792fd/backend/lib/prediction.py
https://github.com/TheSpaghettiDetective/obico-server/blob/df8190ef7500459599ebf398c3d1f653754792fd/backend/lib/failure_detection.py
https://github.com/TheSpaghettiDetective/obico-server/blob/df8190ef7500459599ebf398c3d1f653754792fd/backend/config/settings.py#L486-L500
The per-job reset follows PrinterPrediction.reset_for_new_print():
https://github.com/TheSpaghettiDetective/obico-server/blob/df8190ef7500459599ebf398c3d1f653754792fd/backend/app/models/other_models.py

This derived scoring module is provided under AGPL-3.0-only. The complete text
is in SCORING_LICENSE.txt. Preserve attribution and the corresponding source
when distributing this module. The repository's original license is:
https://github.com/TheSpaghettiDetective/obico-server/blob/df8190ef7500459599ebf398c3d1f653754792fd/LICENSE

Contract:
  Scorer(store, config=None, clock=time.time)
  classify(job_key, detections) -> {suspicious, score, ready, detail}

Pass the raw list from successful Detector.analyze()['detections']; class is
failure, confidence is0..1, bbox has four finite numbers. [] is a valid clean
frame. DO NOT pass [] when a camera/inference request failed: absence is not
a clean frame. Invalid input raises ValueError without changing state.
The source's optional bending_factor/MAX_DET_NUM are not used: both are absent
from its default first-generation setup. Optional detector_sensitivity defaults
to1.0, the official initial sensitivity. Arbitrary raw detector_threshold0.65
is deliberately irrelevant to this judge.

Invoke once per fresh successful frame every10seconds. Official hyperparameters
were tuned for that interval; calling faster compresses their time meaning.
The clock records observed_at for diagnostics, not for inventing interpolated
frames or replacing the source's frame-count math. A restart preserves means,
frame counts and readiness. The Store's mutate transaction updates the state
atomically under the obico_scoring key. No new service or package is needed.

Algorithm, with unchanged official default constants:
  current_p = SUM(confidence of all failure boxes), not MAX.
  EWMA span12; short rolling approximation310; long baseline7200 frames.
  Warning thresholds0.38..0.78, short-mean multiple3.8, sensitivity1.0.
  INIT_SAFE_FRAME_NUM30: a new print needs30 valid frames (~5minutes at10s).
  ESCALATING_FACTOR1.75 is used only for the source's normalized score display.
  suspicious uses official is_failing(..., escalating_factor=1): WARNING level.
  The bot separately confirms3 suspicious frames and starts its5-minute timer.

The score is the official normalized display value0..1, NOT a probability of
print failure. Its ranges map to below-warning, warning and escalation bands;
it is not the raw largest bbox confidence and must not be labelled accuracy.
ready tells the caller that the per-print30-frame warmup has completed.

At a different job_key the per-print count, current_p, EWMA and short mean reset
to zero. Lifetime count and long baseline persist across jobs, exactly like
upstream's reset_for_new_print. This adapts to the same camera environment.
The caller must supply a genuine unique history job ID, not only a filename.
Scoring stores no photos, tokens or private model files.

Important behavior preserved from upstream: the first minutes are assumed safe.
If a print begins with a spaghetti ball already visible, this can be learned as
the baseline and may not trigger an alert. Do not claim all failures are caught.
The source explains this behavior here:
https://www.obico.io/docs/user-guides/how-to-test-failure-detection/

Validation still required:
The official ONNX runtime is described as beta, and this port does not improve
the underlying model. It cannot reconstruct a missed bbox. Public gallery
images test plumbing, not accuracy for this K1C camera. Until real footage and
the reaction path are checked, keep automatic physical reactions in validation
mode. Never lower thresholds solely to make a handful of gallery images pass.
