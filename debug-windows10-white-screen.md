# Debug Session: windows10-white-screen
- **Status**: [OPEN]
- **Issue**: Windows 10 x64 安装 TSP v0.3.5 后，桌面窗口打开但页面白屏。
- **Debug Server**: http://127.0.0.1:7777/event
- **Log File**: .dbg/trae-debug-log-windows10-white-screen.ndjson

## Reproduction Steps
1. 在 Windows 10 专业版 x64 安装 `TSP-Setup-x64-0.3.5.exe`。
2. 启动 TSP。
3. 桌面窗口出现，但内容区域为白屏。

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | WebView2 Runtime 缺失或不可用 | High | Low | **Confirmed**: uploaded log line 71 selects deprecated MSHTML |
| B | PyInstaller 漏收 pywebview/.NET 运行组件 | High | Low | **Rejected**: pywebview starts and deliberately falls back to MSHTML |
| C | 本地页面或静态资源加载失败 | Medium | Low | **Rejected as primary**: backend is ready and URL opens; unsupported renderer explains blank page |
| D | GPU/旧 Win10 图形栈导致渲染失败 | Medium | Medium | **Rejected**: EdgeChromium is not selected at all |
| E | 端口或旧进程竞态 | Low | Low | **Rejected as primary**: line 70 opens the ready server at 127.0.0.1:3018 |

## Log Evidence
Instrumentation added to `backend/app/desktop.py` for:
- WebView2 registry version and Windows build.
- pywebview/pythonnet/clr-loader availability.
- Window URL and GUI event-loop entry.
- WebView loaded event and DOM readiness/length.

Existing packaged `desktop.log` is requested first because pywebview writes its renderer
selection and initialization warnings there without requiring a diagnostic rebuild.

Uploaded pre-fix evidence (`/Users/bytedance/Downloads/desktop.log`):
- Line 33: backend reports 7 capabilities active.
- Line 70: desktop opens `http://127.0.0.1:3018`.
- Line 71: `MSHTML is deprecated`, proving pywebview did not find a usable WebView2 Runtime.
- Lines 72-77: network-backed services continue successfully while the window is blank.

## Verification Conclusion
Root cause confirmed: the Windows installer did not provision the Microsoft Edge WebView2
Runtime. On this Windows 10 machine pywebview falls back to MSHTML, which cannot execute the
modern React/Vite frontend and renders a blank page.

## Fix
- Release workflow downloads the official Microsoft Evergreen WebView2 Bootstrapper and rejects
  it unless the Authenticode signature is valid and belongs to Microsoft Corporation.
- Inno Setup embeds the bootstrapper, checks the documented WebView2 registry client ID, and
  silently installs the Runtime for the current user when missing.
- TSP installation stops with an actionable error if the Runtime installer fails.
- Target release: `v0.3.6`.

## Post-Fix Verification
Automated evidence:
- GitHub Actions run `37932973606` completed successfully.
- Microsoft Authenticode verification passed for the embedded WebView2 Bootstrapper.
- Inno Setup compilation, portable EXE smoke, silent install/run/uninstall, and data retention
  checks passed.
- Published installer SHA-256:
  `7a788a8709a07484b0036b6f7b70106f289cb95d7e346c82416778796c997619`.

Pending Windows 10 user confirmation. Instrumentation remains active only when
`TSP_DEBUG_SERVER_URL` is explicitly set; post-fix collector log has been cleared.

## Iteration: v0.3.6 Startup Regression
Uploaded evidence (`/Users/bytedance/Downloads/desktop(1).log`):
- Line 150: backend is ready and opens `http://127.0.0.1:3018`.
- Lines 151-164: `_open_window` fails before importing pywebview because temporary
  instrumentation calls `importlib.metadata.version("pywebview")` in a frozen build where the
  distribution metadata was not collected.
- Root exception:
  `importlib.metadata.PackageNotFoundError: No package metadata was found for pywebview`.

Conclusion:
- WebView2 provisioning is not the source of this new crash.
- The instrumentation violated its no-behavior-change requirement by allowing metadata lookup
  failure to escape. Fix must make diagnostics exception-safe and add a real Windows GUI smoke
  that enters `_open_window`.

## Iteration: v0.3.7 Fix
Implementation:
- Optional distribution-version diagnostics now catch both missing metadata and unexpected
  metadata lookup errors, so diagnostics cannot prevent window creation.
- PyInstaller explicitly copies `pywebview`, `pythonnet`, and `clr-loader` distribution metadata.
- The release workflow now runs an installed GUI smoke test that enters `_open_window`, requires
  the `edgechromium` renderer, verifies a non-empty loaded DOM, and closes the real window.
- Target release: `v0.3.7`.

Local automated evidence:
- Backend tests: `2721 passed, 7 skipped`.
- Frontend tests: `112 passed`; ESLint and production build passed.
- macOS PyInstaller output contains `_internal/pywebview-6.2.1.dist-info`.
- The frozen executable started v0.3.7, activated all seven market-data capabilities, emitted
  `DESKTOP_SMOKE_TEST_OK`, shut down cleanly, and exited with code 0.

Pending:
- Windows CI must pass portable and installed headless smoke tests plus the installed real GUI
  smoke test before the installer is published.
- Windows 10 user confirmation remains required before this debug session is closed.

### Windows CI Attempt 1
- Run `37946068047` passed all backend/frontend quality gates, PyInstaller packaging, Polars
  runtime checks, WebView2 signature verification, installer generation, and portable smoke.
- The installed executable started v0.3.7, activated all seven market-data capabilities, selected the
  real GUI path, and emitted `DESKTOP_SMOKE_TEST_OK`.
- The GUI quality gate correctly blocked publication because pywebview's `loaded` event fired
  before React had populated `document.body.innerText`; the first probe reported
  `readyState=complete`, non-empty HTML, but `bodyTextLength=0`.
- The GUI smoke now polls the same EdgeChromium window for up to 30 seconds. It still requires
  the expected localhost URL, ready DOM, non-empty HTML, and non-empty body text; a page that
  remains blank still fails publication.

### Windows CI Attempt 2
- Run `37947724523` passed all jobs, including installed GUI verification:
  `DESKTOP_GUI_SMOKE_TEST_OK renderer=edgechromium ready_state=complete
  body_text_length=4 html_length=5568`.
- The published v0.3.7 installer independently matched its Release metadata:
  131486560 bytes, SHA-256
  `031783222c2e49fc791192abd8d4e92a93ae3a0989f1ab5c3b8373377e9bc21a`.
- Post-publication validation found that `latest.json` had captured the Draft Release's
  temporary `untagged-*` asset URL. That URL returns HTTP 404 after immutable publication,
  while the canonical tag URL returns HTTP 200.
- The immutable v0.3.7 asset cannot be corrected in place. The workflow now constructs
  canonical tag URLs directly; final target release is v0.3.8.

### Final Release: v0.3.8
- GitHub Actions run `37949618801` passed prepare, full quality, Windows build, installed GUI
  smoke, uninstall/data-retention checks, and manifest publication.
- Installed GUI evidence:
  `DESKTOP_GUI_SMOKE_TEST_OK renderer=edgechromium ready_state=complete
  body_text_length=138 html_length=31295`.
- Release/tag commit: `4d9891f9906ab8db1584defd674789ecc7648cc4`.
- Installer: 131465063 bytes, SHA-256
  `c8598e2cf62ad0dc54dac3d50e2cde8e1d49bd4e25b023f04f848066ce9ae47f`.
- The downloaded installer and `latest.json` both match GitHub asset metadata.
- The manifest contains the canonical v0.3.8 URL, contains no `untagged-*` path, and that URL
  resolves with HTTP 200.

Pending Windows 10 user confirmation. Keep this session OPEN until confirmed.

## Follow-up: Desktop Registration Bootstrap
- User confirmed that v0.3.8 opened locally but registration code submission returned
  `注册口令尚未配置`.
- Root cause: server deployments bootstrap the registration secret from `.env`, while frozen
  desktop builds intentionally ship without that deployment file.
- The desktop startup path now initializes the default registration secret from a packaged
  PBKDF2 hash and salt only. It never writes or packages the plaintext, preserves users and
  sessions, does not override an existing custom secret, and is disabled for server deployments.
- Frozen desktop smoke now requires `/api/auth/status` to report
  `registration_enabled=true`; target release is v0.3.9.

### Final Registration Fix: v0.3.9
- GitHub Actions run `37953786636` passed all quality, Windows build, portable smoke, installed
  smoke, EdgeChromium GUI, uninstall/data-retention, and manifest jobs.
- Both portable and installed logs contain `desktop registration secret hash initialized`
  followed by `DESKTOP_SMOKE_TEST_OK`; the installed GUI also emitted
  `DESKTOP_GUI_SMOKE_TEST_OK renderer=edgechromium`.
- Release/tag commit: `a2c6f70fb0b88397e839b6c0bdd49270d8f1e02b`.
- Installer: 131467780 bytes, SHA-256
  `d930aca0a64095797d698bf7696190405ec399b9568d3813da93add253fdb35d`.
- The downloaded installer, Release metadata, and `latest.json` match. The manifest URL returns
  HTTP 200 and the `releases/latest` alias resolves to v0.3.9.

## Follow-up: First-Run SMTP Configuration
- v0.3.9 could initialize the registration secret, but a fresh desktop installation still had
  no authenticated route for configuring SMTP before requesting the first registration code.
- v0.3.10 adds a desktop-only, no-user SMTP setup endpoint and registration-page controls.
  The endpoint verifies the registration secret, sends a real test email, and persists settings
  only after delivery succeeds. Non-secret settings go to `preferences.json`; the SMTP password
  remains in mode-0600 `secrets.json`.
- Failed delivery, server deployments, incorrect registration secrets, and installations with an
  existing account cannot persist through this endpoint. The registration-code button stays
  disabled on a fresh desktop install until SMTP has passed the send test.

### Final SMTP Fix: v0.3.10
- Local protocol integration delivered both the SMTP configuration test and registration code to
  an SMTP sink through the real adapter. A separate external SMTP test delivered both messages
  using the configured provider; neither test created an account.
- Local validation: `2729 passed, 8 skipped` backend tests, `113 passed` frontend tests, Ruff,
  TypeScript, ESLint, production build, release YAML, and lock checks.
- GitHub Actions run `37957670531` passed quality, Windows packaging, portable smoke, installed
  SMTP/GUI smoke, uninstall/data-retention, and manifest publication.
- Installed smoke log contains `DESKTOP_SMTP_SMOKE_TEST_OK`, `DESKTOP_SMOKE_TEST_OK`, and
  `DESKTOP_GUI_SMOKE_TEST_OK renderer=edgechromium`. Its SMTP sink captured exactly two messages:
  `TSP 邮件服务配置测试` and `TSP 注册验证码`.
- Release/tag commit: `f43858f7b37298a70d186daf944d8038c6a63649`.
- Installer: 131482031 bytes, SHA-256
  `0cd2e40051f6720b0453cf2b89e822ed61889bead46d70c47f21eede5bf86ce4`.
- The downloaded installer, Release metadata, and `latest.json` match. The stable installer URL
  returns HTTP 200 and the `releases/latest` alias resolves to v0.3.10.
