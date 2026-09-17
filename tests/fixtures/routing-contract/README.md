# Portable routing corpus

These cases are synthetic test data for the matching router source, not a provider
catalogue or a separate API version. The root OpenAPI document remains authoritative.
Production routing data is unchanged.

The endpoint files contain complete requests and reviewed expected responses.
`classify-response.json` holds the exact shared classifier plan, including prompt text.
Tests resolve that one response-file reference. The exporter resolves it too, so archive
consumers receive concrete responses without a template language.

## Archive contents

- `cases.json` contains all scenarios with complete expected responses and exact UTF-8
  `request_body` strings. Send those bytes unchanged for wire replay. `raw_request` marks
  deliberately malformed JSON, and `request_valid: false` marks schema-invalid objects.
- Each case names its HTTP method, path, expected status, and optional headers. Use
  `Content-Type: application/json` when headers are omitted. A 204 response has no body.
- `tables/` contains all six schema-5 synthetic profiles. A case's optional `profiles`
  list restricts the loaded files. Without it, load all six. Mount the selected directory
  read-only and make it readable by UID/GID 10001.
- `openapi.yaml` is the router's authoritative HTTP contract at that source revision.
- `integration-contract.md` is the supplied checksum-verified external attachment.
- `manifest.json` records the archive format, source and release identity, table schema,
  attachment revision, and hashes of every other file. Verify the archive checksum and
  manifest file hashes before using its data.

Compare successful JSON responses exactly, including ranked choice order, identity, effort
omission, and classifier prompt strings. For application errors, compare status and stable
`code`, then validate the complete envelope against OpenAPI. The fixture `detail` is a
representative response for mocks, not a promise to preserve incidental diagnostic wording.
Transport failures and overload before application dispatch can have no JSON envelope.

## Classification failure

The router plans classifier calls but does not execute them or parse raw model output.
The caller validates that output against the classifier-output schema in OpenAPI. If caller
policy permits degradation after invalid JSON, fences, truncation, or invalid labels, omit
classification when routing. Do not fabricate a replacement classification.

Omitted and null classification have paired cases. Auto selects the balanced profile for
the original goal. Explicit modes stay explicit. Authored text supplies deterministic labels
where possible, and every uninferred field stays unknown. A request such as `Proceed.` uses
the all-unknown cell. Eligible choices retain exact efforts and context filtering. No route
or a missing required profile remains an error, not permission to select an arbitrary model.

The two fake choices have deliberately different rankings across profiles and label cells.
Expected responses are reviewed data. Changing the router or synthetic table helper must
not automatically rewrite expectations during tests or export.