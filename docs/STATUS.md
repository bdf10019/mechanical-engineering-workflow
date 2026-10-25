# Scope and status｜範圍與狀態

Release snapshot: **0.2.0, 2026-10-25**. This is a phase delivery; feature work has stopped at the user's request.

## Available

- Six connected rectangular workflow nodes, hover/focus help, click-to-open operation panels and next-step guidance.
- Local create/save/reopen, 1–2 images, dimension/source/change/keep records.
- Fixed input hash, handoff ZIP, structured JSON import and review.
- Optional Responses adapter with per-request consent, ephemeral key and non-refundable one-attempt receipt.

## Not claimed

- Actual cloud model availability, cost, semantic accuracy or live connectivity.
- Photo-to-editable-CAD, arbitrary part separation, modification execution or engineering drawings.
- AutoCAD/SOLIDWORKS compatibility, manufacturing accuracy, safety or approval.
- Independent user study or mobile acceptance.

## Known limits

The local image working copy is limited to 1600px and stripped of metadata, but visible content can still be confidential. Original files remain local. Do not upload unauthorized images.

The network adapter uses a nominal 60-second budget and watchdog; system DNS resolution cannot be reliably interrupted. When resolution returns after expiry, data transmission is blocked. Strict end-to-end cancellation and live-provider behavior remain unverified.

A browser walkthrough observed the unsaved-leave confirmation, but browser-control timeout interrupted the dismissal check. Cancel-and-retain-draft is not counted as a full browser pass. Saved-task reload was verified separately.

Simulated dimensions remain simulated. A response with valid JSON and references is only a suggestion, not a verified model. Acknowledging reading never unlocks engineering approval or the disconnected model/drawing nodes.

The public repository is a deliberately small runtime subset. Local historical experiments and evidence are preserved separately, not rewritten or published here.
