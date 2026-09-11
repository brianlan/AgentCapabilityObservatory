# Private Python Pilot tasks

Six original Python task bundles live in the canonical private task store
selected by the operator as `$ACO_PRIVATE_TASK_STORE` (local git history only,
with no public remote). Their instruction text, initial workspaces, reference
solutions, and hidden verifiers stay outside this public repository.

The manifests changed from v1 to v2 when the immutable instruction source was
declared as `instruction = "workspace/README.md"`. All six v2 bundles were
admitted into the operator-selected clean operational root
`$ACO_DATA_ROOT` with every gate passing, including rebuild, using the public
repository code.

| Task name | Category | Pilot role | Task version id (v2) | Environment digest |
| --- | --- | --- | --- | --- |
| `py-pilot-page-fix` | boundary-condition fix | sanity-control | `e70dab6d1de997cae99518e17548716391c6eba5730c3873697af1d9864a643a` | `1c5e8f821e65de71a6fe86061659d4f3c1b19a060d7125a3beeb6098df05ef41` |
| `py-pilot-ratelimit-fix` | boundary-condition fix | uncalibrated | `8464fe6a68c66a0189f293aadb31e8171233467d9eb1b8137a2458acd96d53d5` | `31e98fb18d98968b0b30c765a9f4fec1bf190f987a3dbda7d5d0f1dd26640929` |
| `py-pilot-shipping-fee` | cross-file feature modification | uncalibrated | `03fadfec693de67c62d7b1829a483b1781a8cf3948ee82dbc4539e2452498ef3` | `eafd15a3203a0e5fda4eb11c6bb8842e989b5627f5807ed88c533d707749665a` |
| `py-pilot-csv-export` | cross-file feature modification | uncalibrated | `6539fc82f5faae7c90ddf69f32cbe3a653d468e96801e215e3dde683f51954cc` | `53e77d27da66a2de7efff58bb3c3f5019d869d482f6bd841d004ab778623d8b8` |
| `py-pilot-log-summary` | structured data processing | sanity-control | `ca890dff0a1f77434df763c897bb3eb0553577245fcdbaa3ee67bd658864e057` | `01644fc292f38bf9fd4ed06658cdd0d6729caa1e2a2c95002bb264cb8118f6d8` |
| `py-pilot-csv-join` | structured data processing | uncalibrated | `b34f55a6fcf22db89139508ffa715505e3bdf9082ab302bde7085e0d7d6ff475` | `3d042269a2aacd3846be8661a5c9bc3dbb3fab3c1c69f95035b3d80b0eed90c9` |

Each TaskVersion declares an explicit default scorer with the matching v2
scorer version (for example, `py-pilot-page-fix-verifier@v2`). A sealed,
verifiable trial queues that scorer once; the manager runs it independently.
The `GET /v1/experiments/{id}` progress object reports the required,
terminal, pending, succeeded, and error counts. An initial scorer error is
terminal for `--wait`, remains `score_error` in results, and never becomes
`pass=false`.

The six admission reports are under `$ACO_DATA_ROOT/admission/`, named by
their TaskVersion ids. To reproduce the local admission state, run this from
the checked-out public repository root:

```bash
export ACO_SRC="${ACO_SRC:-$(git rev-parse --show-toplevel)}"
export ACO_PYTHON="${ACO_PYTHON:-python3.12}"
export ACO_PRIVATE_TASK_STORE="${ACO_PRIVATE_TASK_STORE:?set this to the canonical private task store}"
export ACO_DATA_ROOT="${ACO_DATA_ROOT:-$ACO_PRIVATE_TASK_STORE/operational-data}"

for bundle in py-pilot-page-fix py-pilot-ratelimit-fix py-pilot-shipping-fee \
    py-pilot-csv-export py-pilot-log-summary py-pilot-csv-join; do
  PYTHONPATH="$ACO_SRC/src" "$ACO_PYTHON" -m aco.admission admit \
    "$ACO_PRIVATE_TASK_STORE/$bundle" --data-root "$ACO_DATA_ROOT"
done
```

Admission reports from the readiness run passed `static`, `oracle`, `nop`,
`cheats`, `rescore`, `registration`, and `rebuild` for every bundle. Pilot/Core
promotion remains a separate, human-reviewed operation.
