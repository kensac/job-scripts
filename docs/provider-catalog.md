# Provider catalog and batch readiness

Reviewed September 23, 2026. The executable registry is `src/core/providers/`.
Catalog changes do not change saved models, group grants, enabled boards or
paid results. Rates describe new direct, global API requests, not invoices.

## Current scope

The catalog covers the application's four integrated text providers: OpenAI,
Anthropic, xAI and DeepSeek. It is not a list of every audio, image, video,
research-only or invitation-only model sold by those vendors. New names are
offered only when their structured text protocol is compatible with the client.
Historical identifiers remain priceable. Provider aliases can change their
underlying model without changing our saved settings.

The September read-only model API checks returned twelve Claude models, eight
Grok text models, and two DeepSeek models. Claude's Haiku alias is also retained.
Grok's specialized multi-agent model is not offered by the Chat Completions
client. DeepSeek's two old Flash names remain accepted aliases at the current
Flash price. Sonnet 4.5's catalog API advertises extended capacity, but this
client does not enable its long-context beta; the picker reports 200K.

## Batch contract

| Provider | Vendor support | Published discount | Application transport |
| --- | --- | --- | --- |
| OpenAI | Supported catalog models | 50% | Submit, poll, checkpoint and collect |
| Anthropic | Message Batches | 50% | Not connected |
| xAI | Grok 4.3 and 4.20 variants | 20% | Not connected |
| xAI | Grok 4.5, 4.6, 4.7 and Build 0.1 | Not supported | Blocked |
| DeepSeek | No integrated batch endpoint | No claimed batch discount | Blocked |

`core.batch_capabilities` separates the vendor's capability from a working
application transport. The router, picker and submission boundary must not
interpret a published discount as permission to send a model to the OpenAI
batch client. Unsupported shared-key scheduled work fails closed, without a
full-price synchronous fallback. Personal-key behavior is unchanged.

### Connecting another collector

Anthropic uses `/v1/messages/batches`, Messages request parameters and JSONL
results with succeeded, errored, cancelled and expired outcomes. Its custom IDs
have a different allowed alphabet and length from the posting URLs used in our
request snapshots. Persist a provider-safe ID mapping before submission and
resolve every result to that immutable snapshot, including errors.

xAI accepts JSONL uploads but uses its own batch state and paginated result
envelopes. A compatible live Chat Completions API does not make its batch
responses compatible with OpenAI. Its documented completion target is
best-effort, so the existing OpenAI 24-hour abandonment rule must not apply.

Both integrations require provider-tagged durable batch identity, credential
selection on recovery, normalized per-request usage, partial collection,
duplicate receipt protection and accounting for paid parse failures. A model
switch must not change which provider collects an already-submitted batch.
Transport readiness should change only after these behaviors are tested and a
bounded submit/collect smoke test succeeds on the configured account.

## Pricing boundaries

GPT-5.6 and GPT-6 use the long-context tier above 272K input tokens. Grok's long
tier starts at 200K. These thresholds apply per request, never to the sum of a
batch. Aggregate usage without request boundaries remains unknown.

Anthropic's input count excludes cache reads and cache creation. The live
adapter adds these into the normalized input total and retains the separate
counters. Five-minute and one-hour cache-write rates are exposed independently;
explicit Anthropic cache creation is not enabled by this change. A write receipt
without a supported lifetime-aware rate remains unpriced, never free.

DeepSeek's weekday peak windows and off-peak factor are modeled. Chinese public
holiday exceptions are not modeled, so the estimate can overstate the bill on
those dates. Stored costs are not repriced by this catalog update.

## Official references

- [OpenAI model catalog](https://developers.openai.com/api/docs/models)
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
- [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [Claude batch processing](https://platform.claude.com/docs/en/build-with-claude/batch-processing)
- [xAI pricing](https://docs.x.ai/developers/pricing)
- [xAI batch processing](https://docs.x.ai/developers/advanced-api-usage/batch-api)
- [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/)
