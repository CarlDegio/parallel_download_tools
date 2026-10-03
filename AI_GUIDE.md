# AI 操作手册：跨机器文件传输

## 已确认的用户要求

- 工具目标目录为 `/mnt/liuzihao/download_tools`，其他机器可以复制到任意目录。
- “上传”指发送端共享指定文件/文件夹，接收端主动拉取。没有 HTTP PUT、SFTP 推送或公网发布操作。
- 默认同时 16 个文件，每文件最多 64 个连接。独立编译 aria2 的连接上限为 512。
- 用户最终确认：低速淘汰使用 aria2 原生 `lowest-speed-limit=32K`，不是“连续低速 30 秒”的定制判定。
- **30 秒**用于整文件刷新条件：aria2 RPC 报告该文件的连接数持续低于 32 时，暂停后续传，重新建立连接。
- 所有收发 IP、端口和本机配置放在一个 `config.yaml`。不提交、不推送任何 YAML 或凭据到 Git。
- 不替换 `/usr/bin/aria2c`，不改系统配置，不自动启动公网服务或修改防火墙。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `config.yaml` | 本地收发地址、端口、token、传输参数；初始化自动生成，不在版本控制中 |
| `upload.sh` | 分享路径参数；无参数时使用 YAML 路径或终端选择 |
| `download.sh` | 列表、交互选择、非交互选取和断点续传 |
| `transfer.py` | 白名单文件服务、SHA-256 清单、私有 aria2 RPC 管理、下载监督 |
| `bin/aria2c` | 本地独立动态链接二进制，连接上限 512 |
| `bin/build-info.json` | 源码/二进制 SHA-256、平台和补丁策略记录；下载前检查二进制一致性 |
| `build_aria2.sh` | 验证官方源码包哈希、应用补丁并重新编译 |
| `patch_aria2.py` | 幂等单行修改；只把连接上限 16 改为 512 |
| `vendor/aria2-1.37.0.tar.xz` | 官方源码下载缓存，Git 忽略；构建时自动下载并校验，可另行复制 |
| `vendor/ARIA2_COPYING` | aria2 上游许可证 |
| `.venv/` | 本机 Python 环境，不能直接跨机器复制 |
| `logs/` | 独立 aria2 运行日志和编译日志，仅保存在本地 |
| `tests/`、`test.sh` | 路径/选择/刷新策略与真实回环网络测试 |

## 配置规则

所有脚本默认读自身目录下的 `config.yaml`，不依赖调用时的工作目录。相对配置路径以 YAML 所在目录为基准。CLI 的路径参数以调用者当前目录为基准。

通过 `DOWNLOAD_TOOLS_CONFIG=/secure/machine.yaml ./download.sh ...` 可切换配置，仍不在脚本中硬编码地址。

`sender`:

- `bind_ip`: 监听地址，默认 `0.0.0.0`。
- `port`: 发送端下载服务端口，默认 8080。
- `advertised_ip`: 显示给接收端的 IP/主机名；不用于自动探测公网 IP。
- `tls_cert`、`tls_key`: 同时设置才启用 HTTPS。
- `paths`: 无 CLI 路径时的默认分享列表。

`receiver`:

- `source_ip`: **发送端**可达 IP/主机名，不是接收端本机 IP；不填协议和路径。
- `port`: 发送端端口。
- `scheme`: `http` 或 `https`。
- `ca_file`: 私有 CA 文件；默认使用系统 CA，始终验证证书。
- `output_dir`: 默认 `downloads`，CLI `--dest` 可覆盖。
- `rpc_port`: 默认 0，自动选择本地空闲端口；仅回环监听，随机 RPC secret，不向公网暴露。

`auth.token`: 两端必须一致，至少 16 位字母、数字、下划线或短横线。新生成 token 为随机 32 字节的 URL-safe 表示。不要打印、提交或在普通日志里展示 token。跨机器新生成配置时不会自动得到相同 token，必须安全同步。

`transfer` 主要参数：

| 参数 | 默认 | 语义 |
| --- | --- | --- |
| `max_files` | 16 | 同时调度的文件数；不计算文件夹为一个文件 |
| `connections_per_file` | 64 | aria2 的 `split` 和 `max-connection-per-server`；可设 1..512 |
| `min_split_mib` | 1 | aria2 最小分段大小；文件/剩余区间不足时无法用满连接 |
| `lowest_speed_kib` | 32 | 原生单连接低速淘汰阈值；0 关闭 |
| `refresh_below_connections` | 32 | 严格小于此数才计时；0 关闭整文件刷新；不得高于连接上限 |
| `refresh_low_seconds` | 30 | 低连接数持续多久才刷新 |
| `startup_grace_seconds` | 30 | 每次启动/恢复后先等待连接建立 |
| `refresh_cooldown_seconds` | 60 | 两次刷新间的最短冷却时间 |
| `refresh_min_remaining_mib` | 64 | 剩余量小于此值不因连接少刷新 |
| `max_refreshes` | 20 | 每次运行每个文件的刷新上限，耗尽报错，断点保留 |
| `max_error_retries` | 10 | 任务报错后重新添加 URI 的次数上限 |
| `retry_wait_seconds` | 5 | 重试间隔 |
| `poll_seconds` | 2 | 监督轮询间隔 |
| `save_interval_seconds` | 5 | aria2 保存 `.aria2` 控制文件的间隔 |
| `status_interval_seconds` | 10 | 单文件状态输出间隔 |
| `connect_timeout_seconds` | 15 | 建立连接超时 |
| `network_timeout_seconds` | 60 | 网络超时，**不是持续低速判定时长** |

## 刷新与续传算法

1. 拉取带认证的 `/manifest`；严格验证相对路径、大小、SHA-256、唯一 ID、父子路径冲突。
2. 按用户选择调度，最多 `max_files` 个文件任务。
3. 同一文件在本地 `.download-tools/<id>.part` 中下载，aria2 自行管理随机写入及 `.aria2` 位图。不按文件大小猜测是否完成。
4. RPC 轮询 `tellStatus`，按文件读取 `connections`。这个统计来自 aria2，可能包含正在建立的连接，并不等同于 OS 线程数。实测 512 连接另由测试服务端同时处理的连接数确认。
5. 已过启动保护且持续低于阈值、冷却已结束、剩余数据充足时，调用 `pause`，等待状态真正为 `paused`，再 `unpause`。沿用同一个 GID/断点，不删除任何已下载内容。
6. 下载尾部不刷新：剩余量必须同时达到配置阈值以及 `refresh_below_connections × 2 × min_split_mib` MiB；进度达到 98% 后也不刷新。正在校验完整性的任务不刷新。这些条件避免小文件/尾部连接自然减少造成重启循环。
7. aria2 整任务报错时，删除的是 RPC 历史结果，再以相同输出和 `continue=true` 重新添加 URI；不是删除数据。低速淘汰可能最后表现为错误 5 / No URI available，脚本会走此重试路径。
8. 每文件通过清单 SHA-256 校验后才发布最终文件。发布采用同文件系统硬链接，原子创建且拒绝覆盖已有目标，然后移除隐藏临时文件。无需额外整文件合并空间；目标文件系统需支持普通文件硬链接（Linux 常见本地盘/NAS 支持）。
9. 已有最终文件只有大小和 SHA-256 都正确才跳过；不匹配时报错，不覆盖。
10. Ctrl+C / SIGTERM 停止任务后，给 aria2 最多 30 秒优雅退出并保存断点；强杀时可能重传最近未保存的分段。输出目录加进程锁，避免两个工具同时写同一输出目录。

不要删除、重命名或只复制最终大小的 `.part` 文件而丢掉 `.aria2`。恢复必须使用原输出目录。发送端重启后，路径与内容相同会得到相同 ID，能继续；内容发生变化则使用新 ID，不混用旧断点。旧内容的断点不会自动清理。

## 双机操作范例

先把工具部署到两端。仅在用户指定的目标机器执行命令，不从这份手册推断已获远程 SSH 登录授权。

机器 A 配置自己的监听端口和可达地址，确认分享范围后：

```bash
cd /mnt/liuzihao/download_tools
./upload.sh /data/model.bin '/data/my dataset'
```

机器 B 配置 A 的地址/端口及相同 token：

```bash
cd /mnt/liuzihao/download_tools
./download.sh --list
./download.sh --select model.bin --select 'my dataset' --dest /data/received
```

中断后直接重跑机器 B 的命令。批量作业必须明确 `--all` 或重复的 `--select`；不要依赖非交互 stdin 的选择提示。单独选中文件夹内的文件仍保留其完整相对路径。

反向传输时，在 B 上运行 `upload.sh`，A 的 `receiver` 指向 B；每台机器的单个 YAML 可以同时保留自己的发送配置与对方的接收配置。

不要让全部下载经过单条 `ssh -L` 或启用 SSH 连接复用；跨机器链路会集中到一个 TCP 连接。此工具主动忽略环境代理，让各 HTTP 分段直接连接发送端。

## 构建与迁移

上游版本：aria2 1.37.0，官方源：
`https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0.tar.xz`

固定源码包 SHA-256：
`60a420ad7085eb616cb6e2bdf0a7206d68ff3d37fb5a956dc44242eb2f79b66b`

补丁仅修改 `src/OptionHandlerFactory.cc` 的 `PREF_MAX_CONNECTION_PER_SERVER` 范围上界：16 → 512。原生 `DownloadCommand.cc` 保持不变。初始速度测量有上游内部宽限期；不能把原生低速淘汰解释为“连续 30 秒低速”。

编译目标为 HTTP(S) + JSON-RPC + 校验功能，使用 OpenSSL，禁用不需要的 BitTorrent、Metalink、SFTP 和 NLS。本机 x86_64 动态链接 OpenSSL 3、zlib、c-ares、libstdc++、glibc 等；另一架构/不同系统库必须重编译。不要对共享源码目录同时执行两次编译。

`./build_aria2.sh` 使用已有源码包，否则从官方地址下载；哈希验证失败立即停止。默认 `BUILD_JOBS=8`，可修改环境变量；日志在 `logs/aria2-configure.log` 和 `logs/aria2-build.log`。编译成功才替换本目录 binary，绝不替换系统 binary。

Git 迁移时提交构建脚本、补丁程序、收发脚本、Python 源文件、依赖声明、测试、文档和 GPL 许可证。官方源码压缩包与解压后的构建目录均忽略，由构建脚本按固定版本和哈希恢复。克隆后运行 `./setup.sh`，在新机器生成配置并构建本机二进制；必须填好收发地址，并安全同步两端 token。

`.venv` 不可直接迁移。若目标机器无法下载 GitHub 源码，可单独复制 `vendor/aria2-1.37.0.tar.xz`，无需提交 Git。也可以另行复制兼容平台的 `bin/aria2c` 和 `bin/build-info.json`，否则在目标上构建。Python 依赖仍需可访问的软件源或另行准备的离线包。

YAML 和 token 不得进入 Git。`.gitignore` 已忽略 `*.yaml`、`*.yml`、日志、虚拟环境、下载目录、断点状态、源码缓存和构建产物。若其他工程以前跟踪了某个配置，单靠 ignore 不会取消跟踪；处理版本控制前先检查，不要强制 add 配置。仅将可迁移的工具文件提交版本控制；向远端推送需用户授权。

## 排障与运行限制

- IP/端口不通：核对 `receiver` 指向发送端、服务是否运行、监听 IP、防火墙/NAT；不要自动修改系统网络配置。
- 401：两端 token 不一致。不要把 token 打进错误报告。
- 409：发送源文件在清单生成后变更或消失，停止改写并重启发送端，再获取新清单。
- 首次启动较慢：发送端正在顺序计算文件 SHA-256；只读取所选文件，没有提前复制/切块。
- 连接数上不去：先看文件大小、剩余量、Range 支持、磁盘吞吐和网络瓶颈；连接数不是速度承诺。
- 文件描述符不足：脚本只提高自身及子进程软上限，不改系统永久配置；硬上限不足会报错。本机最初软上限 1024，默认会提高到至少 4096。
- TLS 失败：使用正确域名/IP、证书和 CA，不设置 `check-certificate=false`。
- 默认 HTTP 的 token/数据不加密，只适用于可信链路；公网使用 HTTPS。没有自动申请证书或开放公网端口。
- 共享服务只提供选择清单和文件 ID 路由，不提供任意文件系统路径、shell 命令或写入接口；目录内部 symlink 跳过，文件必须保持不变。客户端使用 `Accept-Encoding: identity`，服务端拒绝压缩协商以避免误读旁边的 `.gz/.br` 文件。
- 无法创建最终硬链接：目标文件系统不支持此发布方式；保留隐藏已下载文件，迁移到支持硬链接的文件系统后操作，不能误报下载成功。
- 工具当前针对 Linux/POSIX（使用资源限制、flock、信号、硬链接）；没有声称支持原生 Windows。

## 验证

执行 `./test.sh`。测试只用本地回环和临时目录，不连接真实远端，不修改用户数据。覆盖：

- 认证、HTTP 206 Range、源文件变化拒绝、压缩副本拒绝；
- 多文件/目录选择、中文/空格名称、空文件和空目录；
- 真正的 aria2 暂停恢复、关闭进程后续传、SHA-256 校验、完成文件跳过；
- 512 个真实并发 HTTP 请求的服务端观测；
- 原生低速淘汰（受控限速服务，观察 Too slow 日志）；
- 低连接数持续计时、冷却、启动和尾部保护；
- 配置不覆盖/600 权限、路径穿越和符号链接拒绝。

测试通过不意味着实际双机的带宽或公网 HTTPS 已经验证。报告时区分本地集成结果和实际远端实测。

上游行为参考：[aria2 手册](https://aria2.github.io/manual/en/html/aria2c.html)、[aria2 官方源码](https://github.com/aria2/aria2/tree/release-1.37.0)。
