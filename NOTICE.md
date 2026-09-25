# NOTICE — 来源与第三方许可

本仓库以 **MIT** 许可发布（见 `LICENSE`）。它从同样以 MIT 发布的
[`enderzcx/agent-gameplay-studio`](https://github.com/enderzcx/agent-gameplay-studio)
中**抽取通用采集组件**而来，原版权归 **Copyright (c) 2026 Sunny**，
抽取时保留同一许可与版权声明。

本仓库只包含**通用代码、合成 fixtures 与文档**：不含任何真实录屏、会话、凭据、
字体或二进制分发物。

## 第三方依赖（各自许可，**不**因本仓库而重新授权）

| 依赖 | 用途 | 许可 | 是否随本仓库分发 |
|---|---|---|---|
| macOS **ScreenCaptureKit** / **AVFoundation**（Apple 系统框架） | macOS 采集与封装 | Apple 系统框架条款 | 否（系统提供） |
| **OBS Studio** | Windows 采集后端所连接的外部程序 | **GPL-2.0** | **否**（用户自行安装；本仓库不 vendor 其源码） |
| **obs-websocket** | OBS 的 WebSocket 接口（协议实现参考其公开协议文档） | **GPL-2.0** | **否**（仅按公开协议通信） |
| `websocket-client` | Windows 后端的运行期依赖（RFC6455 帧） | **Apache-2.0** | 否（由用户环境安装） |
| `websockets` / `pytest` | 仅测试期 | **BSD-3-Clause** / **MIT** | 否 |

**边界说明**：

- OBS Studio 与 obs-websocket 是 **GPL-2.0**。本仓库**没有**复制、改编或分发它们的
  源码，只通过其**公开协议**通信；因此本仓库的 MIT 许可不覆盖它们，也不改变它们的许可。
- 本仓库**不分发**任何字体或编译产物；录制器由使用者在本机自行编译（`tools/macos/build.sh`）。
- 采集到的媒体内容不属于本仓库，也不在本仓库的许可范围内。
