# gh-aw-router

gh-aw-router picks the model for a software task. It builds a provider-neutral classification
prompt, then ranks the exact model choices a caller offers using a routing table compiled
offline. It never calls a provider, executes an agent, fits a model, or stores request history.

The package is self-contained. Training, benchmark evidence, and publication tooling are
maintained separately and are not needed to install or run this service.

## Install and run

Use Python 3.12 and [uv](https://docs.astral.sh/uv/). Run these commands from the directory
containing this README and `pyproject.toml`. Shell examples use Bash syntax. The server runs
in the foreground, so use another terminal for client commands.

```bash
uv sync --locked --dev
uv run gh-aw-router validate-data
uv run gh-aw-router serve --bind 127.0.0.1:8737
```

From a source checkout, the CLI discovers the `routing/` directory. The wheel intentionally
contains code only, so an installed distribution needs tables from the same release's source
archive. The next commands assume the wheel and the extracted `routing/` directory are both
in the current directory.

```bash
python -m pip install ./gh_aw_router-0.1.0-py3-none-any.whl
gh-aw-router --routing-tables ./routing validate-data
gh-aw-router --routing-tables ./routing serve --bind 127.0.0.1:8737
```

Use trusted tables and keep them read-only. Tables are validated at startup, and replacing them
requires a restart.

## CLI

`classify` and `route` read JSON from standard input or from `--input PATH`. The routing-tables
option is global and must precede the command.

```bash
uv run gh-aw-router classify --input examples/classify-request.json
uv run gh-aw-router --routing-tables ./routing \
  route --input examples/route-request.json
```

Command-line options override environment variables, which override defaults.

| Option | Environment variable | Default |
| --- | --- | --- |
| `--routing-tables` | `GH_AW_ROUTER_ROUTING_TABLES` | Checkout's `routing/` |
| `serve --bind` | `GH_AW_ROUTER_BIND` | `127.0.0.1:8737` |
| `serve --log-level` | `GH_AW_ROUTER_LOG_LEVEL` | `info` |

The path may be a directory, which loads every table in it, or a single file, which serves that
one profile. Outside a checkout, a routing path is required. Relative paths resolve from the
current working directory. Bind to another interface only on a trusted private network. The
container binds to all of its own interfaces.

Results go to stdout and errors to stderr. Exit statuses are `0` for success, `2` for invalid
input, invalid table data, file errors, or argument errors, and `3` when routing finds no
eligible model. `--help` and `--version` exit successfully without loading a table.

## HTTP API

The authoritative contract is [openapi.yaml](openapi.yaml), currently API version `0.2.0`. Use
the document from the matching release for validation and client generation. The service does
not serve a generated `/openapi.json`, `/docs`, or `/redoc`.

- `GET /healthz` for readiness
- `GET /capabilities` for the API version, served routing profiles, models, and efforts
- `POST /classify` for a classification prompt and ranked classifier choices
- `POST /route` for ranked task-model choices

Classification and routing return ranked choices drawn only from the supplied candidates.
Routing ignores model-effort pairs absent from the table and returns `no_route` when no
supported, context-eligible choices remain. Unsupported objectives and malformed requests
are still rejected. Neither operation calls a provider or invents a fallback choice.

Both planning requests require `api_version` set to `"0.2.0"`, `repository` in `owner/repo`
form, a nonblank `task_id`, and a `conversation` holding at least one user message with
nonblank text. Keep the repository and task identifiers stable for one task across
classification, routing, and retries. They identify the caller's work but do not select a
policy or create stored state.

To update API `0.1.0` requests, set `api_version` to `"0.2.0"` and move task labels into
`classification.labels`. Include `classification.mode`, using `"unknown"` when no mode was
inferred. Remove `objective.overrides` entirely, even when null, and replace `custom` with
`economy`, `balanced`, `robust`, or `auto`. Requests using API `0.1.0` are rejected.

Application errors use a `code` and `detail` envelope, including unknown paths and unsupported
methods. Schema violations name the failing fields without echoing submitted values. Serving
bounds request bodies to 1 MiB and in-flight requests to 64. A 30-second deadline bounds
asynchronous upload and response waits but cannot interrupt synchronous computation.
Overload can produce a bare 503 before the application runs. Clients must also handle connection
failures and enforce their own timeouts.

Classification follows the table's embedded `classification_choices` list and prefers its first
eligible offered choice. Routing filters the table's ordered identities against the request. Both
operations preserve the caller's exact choices. Models with effort settings are eligible only
with an explicit supported effort. Classification rejects omitted efforts for these models,
while routing skips those choices. Omit effort only for models without effort settings. The
string `"none"` is an explicit effort, not an omission. The service never infers effort settings.
Routing uses `classification.labels` when supplied. When `classification` is omitted or null,
deterministic heuristics infer task type and scope from the last authored user message and
leave complexity unknown.

### Examples

The [classification request](examples/classify-request.json) and
[routing request](examples/route-request.json) are complete JSON examples. With the local
server running, submit them directly.

```bash
curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data-binary @examples/classify-request.json \
  http://127.0.0.1:8737/classify

curl --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data-binary @examples/route-request.json \
  http://127.0.0.1:8737/route
```

`/classify` returns `system_prompt`, `prompt`, and `ranked_choices`. The classifier call uses
`system_prompt` as its only system or developer instruction and `prompt` unchanged as the user
message, with no tools. Its expected output has this shape, defined by
`x-gh-aw-router-classifier-output-schema` in the OpenAPI document.

```json
{
  "labels": {
    "task_type": "fix",
    "scope": "local",
    "task_complexity": "medium"
  },
  "mode": "robust"
}
```

Forward the validated classifier output unchanged in `/route`'s `classification` field,
as the routing example does. `/classify` itself returns a call plan, not this inferred result.
The caller runs the classifier and passes its output to `/route`.

Set `objective.mode` to `"auto"` to use `classification.mode`, which recommends `economy`,
`balanced`, or `robust`. Auto falls back to `balanced` when `classification` is omitted or
null, or its mode is `unknown`. If classification fails, omit it or send null. A supplied
classification must include valid `labels` and `mode` fields or the request is rejected.

An explicit `economy`, `balanced`, or `robust` objective mode always takes precedence over
the classifier's mode. The goal remains the caller's choice of `cost` or `cost-speed`.
`/route` returns exact offered choices in preference order with the same response shape
for automatic and explicit modes.

```json
{
  "ranked_choices": [
    {
      "id": "openai-sol-medium",
      "model": "openai/gpt-5.6-sol",
      "effort": "medium"
    },
    {
      "id": "copilot-luna-medium",
      "model": "github-copilot/gpt-5.6-luna",
      "effort": "medium"
    }
  ]
}
```

Supplying `current_id` places that offered choice first while it remains eligible. For each
routing candidate with a `context_window` in tokens, eligibility uses a character-count
estimate plus 16,000 tokens of headroom. Omitting `context_window` skips that candidate's
context check. This estimate is not a provider tokenizer and does not replace enforcement
of the provider's actual context limits.

## Routing tables

`routing/` holds one file per published profile, named `{goal}-{mode}.json` for the goals `cost`
and `cost-speed` and the modes `economy`, `balanced`, and `robust`. Each file contains the
ordered provider/model or provider/model-effort identities for every label cell and a separate
classifier ranking. Prices, benchmark evidence, fitted parameters, and provider credentials stay
outside the runtime package.

The service loads every table it is given and picks one per request from `objective.goal` and
the resolved mode. Auto is a selection rule, not a separate table profile. `/capabilities`
lists only the fixed profiles that are loaded. For all automatic choices to work, load the
three mode tables for each supported goal. The bundled `routing/` directory contains all six.

A resolved profile that is not served fails with `invalid_request`, including when the
balanced fallback table is missing. An inferred `robust` mode never silently becomes
`balanced` because a table is unavailable. Single-file deployments work only when the
resolved profile matches that file.

Tables loaded together must agree on their repository, model catalogue, and classifier
order, so they differ only in their routing order. Select a directory or a single file with
`--routing-tables` or `GH_AW_ROUTER_ROUTING_TABLES`, and mount or publish generated tables
read-only.

Only schema 5 is supported. A schema-5 table declares one fixed `cost` or `cost-speed` profile
and carries no default-effort aliases.

## Container

Build from this directory. The Docker build context is self-contained.

```bash
docker build --tag gh-aw-router:dev .
```

The image bundles every published table under `/routing` and serves all six profiles. It runs as
user `10001:10001` and needs no credentials or outbound network. This invocation uses a private
network, a read-only filesystem, and no published host port. The service listens on port `8737`
inside the network.

```bash
docker network create --internal gh-aw-routing

docker run --detach \
  --name gh-aw-router \
  --network gh-aw-routing \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --stop-timeout 35 \
  gh-aw-router:dev
```

Only trusted clients such as the API proxy belong on this network, not the coding agent.
`GET /healthz` returns `204` when ready, and `GET /capabilities` describes the loaded profiles.
To replace the bundled tables, add these options before the image name.

```bash
--mount type=bind,src=/trusted/routing,dst=/mnt/routing,readonly \
--env GH_AW_ROUTER_ROUTING_TABLES=/mnt/routing
```

## Contributing and security

[CONTRIBUTING.md](CONTRIBUTING.md) covers development conventions and checks. The package
version, the planning API version, and the table schema version change independently, so read
all three from `/capabilities` and the loaded table rather than assuming they move together.

The service intentionally has no authentication or TLS. Keep it on a private network. Report
vulnerabilities through [SECURITY.md](SECURITY.md). The code is licensed under [MIT](LICENSE).
