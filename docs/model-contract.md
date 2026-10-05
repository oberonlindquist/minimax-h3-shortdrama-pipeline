# MiniMax H3 model contract

This table is intentionally short. Update it only after checking the live [Argolink model page](https://argolink.io/gh-minimax-h3-shortdrama-pipeline).

- Model ID: `minimax-h3`
- Argolink model page: `https://argolink.io/gh-minimax-h3-shortdrama-pipeline` (tracked shortlink; destination is the model page)
- Endpoint family: `/v1/videos/generations`
- Listed video tiers: 768p `$0.08`/second, 2k `$0.13`/second.
- Base generation rate: `$0.08`/second before any tier multiplier.

## Request boundary

- Authentication: `Authorization: Bearer $ARGOLINK_API_KEY`.
- Base URL: `https://api.argolink.io`.
- Model ID: `minimax-h3`.
- Endpoint: `/v1/videos/generations`.
- For media jobs, save the returned task identifier, poll the documented task endpoint, and download the final URL before its retention window ends.

Do not copy prices or limits from third-party lists into this file; the model page and catalog are the current source of truth.
