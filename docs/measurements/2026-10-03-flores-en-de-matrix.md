# FLORES-200 EN-DE model comparison

Quality mode: reference

| Model | Cost / 1M tokens in+out (USD) | Known run cost (USD) | chrF | Preservation % | Recorded bulk requests | Tokens in / out |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gpt-4o-mini | $0.1300 | $0.004092 | 71.37 | 73.68% | 1 | 29766 / 1698 |
| gpt-4o | $4.4921 | $0.025165 | 70.84 | 73.68% | 1 | 4114 / 1488 |

- Cost is the application estimate for provider-reported bulk and triage usage; ambiguous or unrecorded usage is excluded, so this is not a billing total.
- Cost per million is observed known cost divided by all reported input and output tokens combined, multiplied by 1,000,000; it is not a provider price tier.
- Requests counts recorded bulk chunk attempts only. Triage request counts are not persisted and are excluded; glossary lookup makes no provider requests.
