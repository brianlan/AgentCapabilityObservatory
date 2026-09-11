# Private Python Pilot tasks (issue #40)

Six original, self-authored Python task bundles exist in the local private
task store at `/ssd4/aco-private-tasks` (local git history only, no public
remote). Their instruction text, initial workspaces, reference solutions,
and hidden verifiers are intentionally NOT in this public repository.

Each bundle was admitted through `aco.admission` into the local data root
with every gate passing: oracle 3/3, nop 0/3, identical repeat scoring, all
declared cheat cases failing, and clean public/image leak scans. Reports
live under `<data-root>/admission/`, keyed by the registered task version id
below. The two sanity-control tasks exist to validate the pipeline only; the
other four have deliberately unclaimed difficulty until real Pilot trials
calibrate it. Pilot/Core promotion is a separate, human-reviewed step.

| Task name | Category | Pilot role | Task version id (v1) |
| --- | --- | --- | --- |
| `py-pilot-page-fix` | boundary-condition fix | sanity-control | `79e946a947056f8476cd60d891ac6cabb1c90c53e33005e4e293835901a7722f` |
| `py-pilot-ratelimit-fix` | boundary-condition fix | uncalibrated | `7736c6d229c38d2ac890bbe3e6d622641202e0a74e58d51659c4e4ca18fc` |
| `py-pilot-shipping-fee` | cross-file feature modification | uncalibrated | `ed8729b388b28de7cb395b26b830c4dfede2d239c993e9af895f42a20eee09dc` |
| `py-pilot-csv-export` | cross-file feature modification | uncalibrated | `512a417ea2bc82ed1da0982052bd7325a0fb5df122f5b198ca6a6e1afb5b6ace` |
| `py-pilot-log-summary` | structured data processing | sanity-control | `fd65f572c9ac7862e94772ed062dd9e08be28e572d0f0b0613a63660918fc7ae` |
| `py-pilot-csv-join` | structured data processing | uncalibrated | `86e7b08dcdaa4a9bb39a9347c60cda18e40e22c31a157c5435cef0a458a5c45d` |

Re-running admission (regenerates the report for a bundle):

```bash
ACO_MANAGEMENT_TOKEN=<token> python -m aco.admission admit \
    /ssd4/aco-private-tasks/<bundle> --data-root <data-root>
```

The admission run is idempotent: bundle digests and the registered version
ids are content-addressed, so a rerun rewrites the same report files.
