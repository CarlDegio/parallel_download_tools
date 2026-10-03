# download_tools

两台 Linux 机器之间传输选中的文件或目录。发送端提供有认证的 HTTP(S) 下载，接收端使用本目录内的 aria2 特殊编译版。不会替换系统 aria2。

默认同时下载 **16 个文件，每文件最多 64 个独立连接**。特殊版允许每文件最高 512。低速阈值默认 32 KiB/s，使用 aria2 原生淘汰逻辑；某文件连接数不足 32 持续 30 秒时，暂停后续传以刷新连接。小文件、启动阶段和下载尾部有保护。

## 本机使用

编辑本目录的 `config.yaml`。所有 IP、端口、认证 token 和传输参数都在这里；该文件权限为 600，已加入 `.gitignore`。两台机器的 `auth.token` 必须一致。不要把配置或 token 上传 Git。

发送机器设置 `sender.bind_ip`、`sender.port`、`sender.advertised_ip`，然后选中要分享的路径：

```bash
./upload.sh /data/model.bin /data/dataset
```

不传路径时，读取 YAML 中的 `sender.paths`；仍为空时在终端输入路径。目录递归分享，目录内部的符号链接跳过。发送前计算 SHA-256，巨大的文件会先经历一次完整磁盘读取。保持这个进程运行。

接收机器设置 `receiver.source_ip`、`receiver.port`、`receiver.scheme`，然后：

```bash
./download.sh --list                              # 查看远端可选项
./download.sh                                     # 按编号选择文件或目录
./download.sh --select model.bin --select dataset  # 非交互选择
./download.sh --all --dest /data/received          # 下载全部
```

输出保留所选根目录名，例如分享 `/data/dataset` 后得到 `/data/received/dataset/`。同名但不同内容的已有目标文件会报错，完整且 SHA-256 一致的文件会跳过。

Ctrl+C 后等待保存断点；恢复时运行相同命令，使用同一个输出目录。**不要删除输出目录中的 `.download-tools/`**，其中保存 `.part` 和 `.aria2` 断点。最终文件只在 SHA-256 校验通过后出现。无需预切分或事后合并整个文件。

HTTP 不加密；公网传输请配置 `sender.tls_cert`、`sender.tls_key` 和 `receiver.scheme: https`，证书须匹配接收端配置的主机名/IP。私有 CA 可配置 `receiver.ca_file`。脚本不关闭证书验证。网络/防火墙放行发送端配置的端口。

## 在另一台机器安装

克隆仓库后进入工具目录，运行下面的安装命令即可。仓库包含脚本、Python 源文件、测试、文档、`requirements.txt` 和 `vendor/ARIA2_COPYING`；不包含 aria2 源码压缩包、编译产物或机器配置。构建脚本会自动下载固定版本的官方源码并校验 SHA-256，然后应用 512 连接补丁。

不要复制 `.venv`、`logs`、下载数据或构建中间产物。配置可单独通过可信渠道传递，或者在新机器生成后填入相同 token。若目标机器无法访问 GitHub，可另外复制本地 `vendor/aria2-1.37.0.tar.xz`；它是可选的下载缓存，不提交 Git。安装 Python 依赖仍需可访问的软件源。

需要 Python 3.10+、C++ 编译器、make、pkg-config、curl、xz、OpenSSL/zlib 开发库。Ubuntu 可安装：

```bash
sudo apt-get install build-essential pkg-config libssl-dev zlib1g-dev libc-ares-dev python3-venv curl xz-utils
./setup.sh
```

已有 Conda Python 时可用 `PYTHON=/path/to/conda/bin/python ./setup.sh`。本机使用 `/mnt/liuzihao/miniconda3/bin/python`，因为系统 Python 缺少 venv 的 ensurepip。

`setup.sh` 在 `.venv` 中安装依赖，生成配置但不覆盖已有配置，必要时编译独立 aria2。默认使用 PyPI，可通过 `DOWNLOAD_TOOLS_PIP_INDEX` 指定镜像。切换平台/系统库后运行 `./build_aria2.sh`，不要假设现成二进制在不同机器一定兼容。

```bash
./bin/aria2c --help=max-connection-per-server  # 应显示 1-512
./test.sh                                   # 本地回环集成测试
```

更详细的部署、参数、排障与测试说明见 [AI_GUIDE.md](AI_GUIDE.md)。
