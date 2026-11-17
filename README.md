<p align="center">
  <img src="assets/chombo-logo.png" alt="Chombo logo" width="360">
</p>

# Chombo v0.5.2

Chombo supports Japanese-mahjong learning, hand analysis, scoring, local play and Replay. The interface and Skill instructions are currently primarily Japanese.

## Available features

- Render hand diagrams from compact tile notation.
- Analyze shanten, tenpai, waits and improving tiles (ukeire).
- Calculate payments from the selected rules, fu, han, win method, dealer status and honba.
- Play a single hand or an east-south match with one human and three Chombo seats.
- Generate and save a hand played by four Chombo seats.
- Create, validate and confirm custom rules within the supported dimensions.
- Restore checkpoints and replay supported Chombo records.
- Ask about fixed positions during or after play, subject to viewing permissions.
- Calculate placement and target-point conditions from explicit inputs.

## Prerequisites

- Python 3.9 or later.
- Pillow for hand-image generation.
- A Codex desktop version supporting local plugins, if using the Codex route.

Download the ZIP and matching checksum identified in the distribution instructions. Verify the checksum, then extract into a new directory. Standalone use does not require Codex, Node.js or Git.

## Use through Codex Skills

Install the complete extracted bundle. Copying only the Skills folder will not work.

In the marketplace root's `.agents/plugins/marketplace.json`, set `plugins[].source.path` to a relative path beginning with `./` that points to the complete bundle inside that marketplace root. Register the marketplace with:

```text
codex plugin marketplace add <absolute-marketplace-root>
```

In the desktop Plugins screen, select the local source and install and enable Chombo. Confirm that Chombo v0.5.2 is installed and its ten Skills are enabled.

Official installation instructions: https://developers.openai.com/plugins/build/plugins#install-a-local-plugin-manually

| Skill ID | Purpose |
|---|---|
| `render-hand` | Render a hand diagram. |
| `analyze-hand-completion` | Analyze hand completion and shape. |
| `calculate-mahjong-score` | Calculate payments. |
| `quick-game` | Start one hand. |
| `hanchan-session` | Start an east-south match. |
| `configured-match-session` | Start a match from a specified position. |
| `generate-autonomous-hand` | Run four Chombo seats through one hand. |
| `replay-mahjong-hand` | Replay a specified record. |
| `calculate-rank-conditions` | Calculate placement and target-point conditions. |
| `report-chombo-bug` | Prepare a reviewed report for sending through Codex. |

```text
Use $analyze-hand-completion to analyze 123456m789p5567s.
Use $quick-game to confirm the rules and start one hand.
Use $replay-mahjong-hand to replay the specified Chombo record.
```

Replay opens supported Chombo records. Keep saved records unchanged; editing their contents can prevent them from opening.

## Standalone Runtime

Open a terminal in the extracted Chombo folder. Replace the example Python path below with your installed Python executable. Use that same executable to check Pillow and start Chombo.

```powershell
$chomboPython = 'C:\PATH\TO\python.exe'
& $chomboPython -B -c 'import sys; import PIL; print(sys.version); print(PIL.__version__)'
& $chomboPython -B -m engine.runtime serve --port 0
```

On macOS, also use the chosen Python's absolute path:

```sh
CHOMBO_PYTHON=/absolute/path/to/python3
"$CHOMBO_PYTHON" -B -c 'import sys; import PIL; print(sys.version); print(PIL.__version__)'
"$CHOMBO_PYTHON" -B -m engine.runtime serve --port 0
```

The default data directories are `%LOCALAPPDATA%\Chombo\data` on Windows and `~/Library/Application Support/Chombo/data` on macOS. This folder contains your saved data; back it up regularly.

Open the local URL in the READY output. Leave the startup terminal open. Use another terminal with the same Python, extracted directory and settings to stop the Runtime, and set the Python variable again in that terminal. Closing the browser does not stop the Runtime.

Windows PowerShell:

```powershell
$chomboPython = 'C:\PATH\TO\python.exe'
& $chomboPython -B -m engine.runtime status
& $chomboPython -B -m engine.runtime stop
```

macOS:

```sh
CHOMBO_PYTHON=/absolute/path/to/python3
"$CHOMBO_PYTHON" -B -m engine.runtime status
"$CHOMBO_PYTHON" -B -m engine.runtime stop
```

Only one Runtime can use a data directory at a time. Startup stops on port conflicts or unwritable paths.

Use the browser screen to start a game, save or restore a checkpoint, open Replay, edit rules, analyze a hand, or ask about a position. For placement and point conditions, enter the values in the form; they are not filled from your game or Replay automatically.

## Optional external LLM connections

### BYOK chat is a demo under evaluation

BYOK lets you try an external AI provider with your own API key. This is a demo with limited answers, not a general-purpose chat assistant. Knowledge questions currently cover shanten, ukeire and shape-only tenpai. Position questions use the analysis and information available to your seat.

Broader conversation is planned for v0.5.3 or later. A successful connection check does not mean every kind of question is supported.

OpenAI, Anthropic and Gemini connections are optional. Basic features and the existing Codex route require no additional Chombo API key.

Enter a provider key in the local settings screen. The Runtime holds it in memory for the browser session; it is not saved to disk or browser storage. Deletion, expiry or a Runtime restart invalidates it.

Preview the external payload and approve every send. Retrying requires new permission. The key owner pays the provider's API charges. Model access and billing must be checked separately from a ChatGPT subscription.

Successful live responses across all providers, response times and costs have not been verified. Check your provider account's model access and billing before use.

## Before you play

- Choose the rules before starting a game. Custom rules are limited to the options offered in the editor.
- Play is local and supports four-player Japanese mahjong, as a single hand or an east-south match. Online multiplayer and east-only matches are not available.
- Replay accepts supported Chombo records; importing records from other mahjong services is not available.
- Chombo is not intended to provide strong strategic advice.
- The JPML rules use an existing snapshot. The official adopted-yaku page was unavailable, so those rules have not been checked for recent changes.

## Known issues

These issues may affect use. Some were reported on Windows or in command-line tools and may not occur in every environment.

| Area | Issue and impact | Current workaround |
|---|---|---|
| Wait display in all-hands-visible Replay | Other players' visible hands are omitted from remaining-tile counts and exhausted-wait dimming (#277). | Check public tile locations separately; do not rely on the displayed counts when all hands are visible. |
| Spectator match results | The result dialog may remain after selecting `閉じる` (Close) (#278). | Close the finished tab. Stop the Runtime separately. |
| Replay position slider | One left/right arrow key press may advance two events (#275). | Use `戻る` (Previous) / `進む` (Next) for one-event movement. |
| Multiple-option chi in single-hand play | A partially selected call may be cleared, making completion difficult (#269). | No reliable workaround is confirmed for that screen. Stop the game if selection cannot be completed. |

<details>
<summary>Additional issues when using command-line tools</summary>

| Area | Issue and impact | Current workaround |
|---|---|---|
| Placement-condition CLI | Setting score bounds (`score_bounds`) can cause an error (#268). | Avoid calculations requiring those bounds. Omitting the bounds does not answer the same constrained question. |
| Point-condition CLI | A rule reference (`rule_ref`) may be accepted even when its ruleset or version does not match (#273). | Check all reference fields against the selected profile before running. A successful response does not guarantee version agreement. |
| Autonomous-generation CLI | Selecting a missing rules file (`--rule-ref`) causes an error (#272). | Check that the file exists and is readable. No record is produced on failure. |
| Winning-qualification CLI | Passing `own_discards: null` causes an error (#270). | Supply the actual own discards as an array; use an empty array only if there are none. |
| Highest-value scoring CLI | Mixing candidates with different honba compares honba-inclusive amounts and can lose the highest ordinary score or tied candidates (#267). | Do not compare candidates with different honba together. Occurrence in an actual same-position, same-honba game is not confirmed. |

</details>

Issue numbers refer to the development repository. Include the number in a support request even if you cannot access that repository.

### Compatibility

Windows and macOS have been checked in specific environments. Other processors and every Python/Pillow combination are not guaranteed. Automatic Skill selection in a new Codex chat and use with a screen reader have not been verified. If Codex does not select a Skill, name it explicitly as shown above.

## Licenses and dependencies

Code and general documentation use Apache License 2.0. Mahjong knowledge, display data and distributed media use CC BY 4.0. See `LICENSE_SCOPE.md`, `NOTICE`, `ASSET_RIGHTS.md` and `LICENSES/CC-BY-4.0.txt` for details and attribution. These licenses do not grant trademark rights to the Chombo name or logo.

Web dependency inventories and their complete license texts are at `ui/.output/public/_nuxt/third-party-notices.json`. Original copyright and license conditions continue to apply.

## Errors and support

[Open a GitHub issue](https://github.com/ImmortalFruit/chombo_runtime/issues/new) or contact [chombo.feedback@gmail.com](mailto:chombo.feedback@gmail.com). Use email if GitHub is inaccessible.

Include the version, OS/CPU, reproduction steps, expected and actual results, and error code. Do not send API keys, control tokens, personal information or complete conversations. Reports are not automatically forwarded to email, and errors are not automatically sent.

In Codex, `$report-chombo-bug` prepares a report for review before sending through a connected GitHub or email service. If access is unavailable, it returns a draft and manual instructions. Standalone users should contact the destinations above manually.
