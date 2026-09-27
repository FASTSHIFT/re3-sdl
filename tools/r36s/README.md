# tools/r36s - R36S (aarch64) 构建

用 Docker + qemu 在 x86_64 宿主机上编 aarch64 版 reVC。环境：Ubuntu 20.04 arm64，glibc 2.31。详见仓库外的 `docs/02-编译环境搭建指南.md`。

## 一次性准备

```bash
sudo apt install -y docker.io qemu-user-static   # binfmt 需带 F 标志
sudo usermod -aG docker "$USER"                  # 重新登录后生效
git submodule update --init --recursive
docker build --platform linux/arm64 -t revc-r36s-builder:focal tools/r36s
```

## 编译

```bash
tools/r36s/build.sh                 # Release
tools/r36s/build.sh RelWithDebInfo  # 带符号
```

产物位置：`build-r36s/src/reVC`。

参考耗时（16 核）：

| 场景 | 耗时 |
|---|---|
| 首次全量编译（qemu） | 约 7 分钟 |
| ccache 已热时全量重编 | 约 20 秒 |
| 改一个文件后增量编译 | 几秒 |
