# Async video lifecycle

`minimax-h3` uses `/v1/videos/generations`. Treat video creation as a job lifecycle:

1. Validate the prompt and model-specific fields against the [Argolink model page](https://argolink.io/gh-minimax-h3-shortdrama-pipeline).
2. Submit once and persist the returned task ID.
3. Poll `GET /v1/tasks/{id}` with bounded backoff.
4. Stop on a terminal state, then download the returned media URL before its retention window ends.

## Current catalog note

4–15 seconds; 768p or 2k; one output per request; a first frame alone or with a last frame, and the video takes the first frame's shape; up to 9 images, 3 videos and 3 audio clips; reference clips 2–15 seconds each and 15 in total, and reference video seconds are billed like output seconds; text: 21:9, 16:9, 4:3, 1:1, 3:4 or 9:16; references may also use adaptive; prompt required

A timeout leaves the task outcome unknown. Query the task status before considering a retry. Never hide a failed terminal state by returning a fabricated media URL.
