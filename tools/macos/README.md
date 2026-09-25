# tools/gamerec — macOS app-level audio + picture recorder

Records **one target application's** audio and picture together, using ScreenCaptureKit:

- `SCContentFilter(display:includingApplications:)` — filtered to the single target app
- `capturesAudio = true`, `captureMicrophone = false` — the target app's audio, **not** the mic,
  **not** other apps
- `AVAssetWriter` (H.264 + AAC) — written directly so real signal can be measured, not assumed

macOS 15+. Source only; you compile it. Nothing here is a signed binary.

## Build

```bash
./build.sh          # → tools/gamerec/build/GameAVRec.app   (gitignored)
```

Ad-hoc signed. If signing fails, the tool still works, but its TCC identity may be unstable — macOS
may ask for Screen Recording permission again.

## Use

```bash
# Replace with YOUR target app's bundle id. There is no built-in default.
export GAME_BUNDLE_ID="com.megacrit.SlayTheSpire2"

./record-game.sh preflight     # permission / target match / window on screen / capture settings
./record-game.sh start --out run.mp4 --duration 60 --focus-log run.focus.jsonl
./record-game.sh verify run.mp4
```

**There is no built-in default target, deliberately.** Capturing the wrong app produces a file that
looks fine and is useless, so `--bundle-id` / `--app-name` / `--pid` (at least one) is mandatory and
the missing-target case exits `64` before any permission prompt. A target that is given but does not
match also fails — the recorder **never** falls back to some other app.

`GameAVRec` can also be called directly:

```bash
APPID="com.megacrit.SlayTheSpire2"          # replace with your target app's bundle id

GameAVRec --probe --bundle-id "$APPID"                    # JSON: permission, displays, target, windows
GameAVRec --bundle-id "$APPID" --out x.mp4 --duration 20 --json x.metrics.json
GameAVRec --bundle-id "$APPID" --no-video --out x.m4a --duration 20
GameAVRec --help
```

### Safety defaults (fail closed, not fail quiet)

These three are behavioural guarantees, not suggestions, and each is covered by an always-on offline
test — none of them needs a granted permission to be testable:

| Guarantee | Behaviour | Test |
|---|---|---|
| **Explicit target, no fallback** | No selector ⇒ usage error `64`, raised *before* any permission prompt. A selector that does not match ⇒ error `2`. The tool never captures a different app. | `O1`, `O6`, `P1` |
| **No overwrite by default** | `--out`, `--json`, `--log`, `--status` all refuse to clobber existing files (`exit 3`, byte-for-byte untouched). Requires explicit `--overwrite`. This check runs before the recorder is constructed, so it holds even without permissions. | `O7`, `O8` |
| **Bounded stop** | 30 s startup bound and a shutdown deadline of `duration + 30 s`. Both end in a terminal state (`exit 4`) with a status file if one was requested — never an indefinite hang. | `P7` |

`--focus-log` samples the frontmost app and whether the target's window is on screen, every 0.5 s,
**without stealing focus or activating anything**.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `2` | runtime failure (target not matched, writer failure, no display) |
| `3` | refused to overwrite existing output |
| `4` | startup or shutdown timeout (bounded; never hangs forever) |
| `64` | usage error — including "no target specified" |

## What `verify` actually proves

`verify` reports **only measurements it took**:

- an audio track exists, plus single-pass per-second peak levels covering the whole file
  (1 s buckets, `-66 dBFS` signal floor) and the voiced-bucket ratio;
- video frames keep arriving (PTS gaps), **and** sampled frame content actually changes (md5) —
  because "PTS keeps advancing" is not the same as "the picture is fresh";
- container-level audio vs video duration difference.

It prints, in its own output, what it does **not** prove: audio/video **sync** (no action-level
check), picture quality, or that the sound came from the target app. A silent track is reported as
silence, not diagnosed — if a game mutes itself when unfocused, the recorder cannot tell that apart
from a capture failure.

## Tests

```bash
./tests/regression.sh                                    # offline group only (no permissions needed)
GAME_BUNDLE_ID="com.megacrit.SlayTheSpire2" ./tests/regression.sh   # + permission-required group
```

The suite is split on purpose:

- **Offline group — 8 cases, always runs, no permissions and no display needed:** the missing-target
  usage error and its exit code; `--help`; silence detection; one-frame video; `--expect` mode
  mismatch; the `GAME_BUNDLE_ID` gate in `record-game.sh`; and the two **no-overwrite** gates
  (`--out` and `--log`), which are testable offline precisely because they run before any permission
  request.
- **Permission group — 7 cases** (needs `GAME_BUNDLE_ID` + Screen Recording access + a display + an
  app that actually emits audio): target-miss failure path; `--overwrite` actually re-recording;
  real audio capture; `--no-video` audio mode; relative timestamps and status self-consistency;
  log tee growth; and bounded early-stop shutdown.

Current offline result: **20 passed, 0 failed, 7 skipped.** Without `GAME_BUNDLE_ID` the permission
group reports **SKIP** — it does not report a pass it did not earn.

## Known boundaries

Verified on the author's machine: capture works while the target app is **not frontmost**, as long
as its window is **on screen**; app-level isolation holds (a second captured app measured digital
silence).

**Untested:** minimized, hidden, or locked screen; muted output; restarting the game mid-capture;
multiple displays; Windows and Linux. Action-level audio/video sync is untested everywhere.
