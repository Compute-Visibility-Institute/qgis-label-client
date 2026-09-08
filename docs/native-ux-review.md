# QGIS UX review — 2026-09-08

Status: recommended application choices approved and implemented locally. Changes
are not committed or released. A valid-time change now preserves unsaved edits and
restores the previous controls, leaving Save/Discard to QGIS's editing workflow.

## Observed in real QGIS 3.44.13

Before the changes, at a 430px dock width the content had a 518px minimum width. The Connect button was
offscreen; explanatory paragraphs were clipped. The bottom status message required
scrolling past all the controls. These issues were reproduced with real widgets
offscreen using a temporary profile, without modifying the user's QGIS session.

The updated dock passes real-widget checks at **320px and 430px**, with no horizontal
overflow and Connect visible. Connection actions occupy two rows; wrapped forms,
short instructions and theme icons use native controls. Status and operation
progress now stay below the scroll area. Date-query transport options are collapsed
under Advanced. Confirmed read-only access continues to disable Publish through
connection/busy state changes.

The current grouped checkbox implementation passes real Qt testing: selecting one
mode returns all three geometry collection IDs; partially loaded groups remain
unchecked; fully loaded groups precheck correctly. The session handoff's tuple
marshaling concern is stale: current code already stores comma-separated strings.

## Implemented layout and behavior

| Area | Proposed change | Acceptance check |
| --- | --- | --- |
| Connection | Responsive form and separate action rows; familiar Connect, Refresh and Copy theme icons | Every action accessible at 320px and 430px dock widths without horizontal scrolling |
| Daily work | Short labels and one-sentence instructions; protocol/storage explanations in tooltips or Advanced | Sign in, choose dataset and add layers without reading implementation details |
| Time | Keep valid-time and historical-belief controls distinct | Analyst can tell which date describes the ground and which describes the recorded belief |
| Feedback | Persistent status/progress below the scrolling area; use existing QgsTask progress and supported cancellation | Scrolling never hides the current operation; cancellation reports partial publish results |
| Errors | Native message bar with clear action and accessible details | Recoverable errors do not interrupt with an unnecessary modal dialog |
| Session renewal | Retry plugin-owned GET requests once using renewed credentials and original request context | No retry loop, no automatic publishing or edit-save replay, no misleading sign-in request after successful renewal |
| Permissions | Confirm caller scopes with whoami; native read-only flag for confirmed readers | Readers cannot spend time drafting edits that will be refused; unknown/stale permission does not lock writers out |

Existing confirmations for publishing, conflicts and irreversible actions remain.
Use QGIS theme icons and native controls rather than custom styling or fixed colors.
Native provider requests are not plugin-owned tasks: renewal messaging must not claim
to replay a native layer save or hide its outstanding edit buffer.

## Style publishing

Capture each source layer's supported style on the main thread. Let the analyst
include or omit it independently of publishing the data. Preview the existing and
proposed style, with omissions/refusals in expandable details. Carry these outcomes
into the report, including partial or cancelled publishes.

Preserve registry fields outside the captured vocabulary; omit unchanged proposals.
Conflicting styles mapped to one class require an explicit human choice. Never
select the first layer automatically. A proposal-only report should provide copyable
JSON and an administrator handoff through the existing class editor and its normal
reason/Save workflow. Applying class changes during publishing is a separate user
choice and needs explicit administrator checks and concurrency handling.

The publish preview now hides supporting columns behind Show layer details and
shows native current/proposed swatches in Review style proposals. The report's
Copy style proposals action feeds the class console's Import QGIS style proposal
control. Import validates the class and captured baseline, changes only the local
preview, and retains the normal reason and Save action. Conflicts are excluded from
transfer until resolved by choosing which layer's style to include.

The public plugin release is unchanged. These local changes have not been
committed, pushed or released.
