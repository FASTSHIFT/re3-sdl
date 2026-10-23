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

## 归档工具（gta3.img / gta3.dir）

纯 Python 3 脚本，在 re3-sdl 仓库根目录运行。`txd_compress.py` 的 ASTC 编码需要 `astcenc` 和 PIL。

| 脚本 | 用途 |
|---|---|
| `img_convert_astc.py` | 整包转换：把所有 D3D8 TXD 转成 GL3 原生 ASTC，其余条目原样透传。`python3 tools/r36s/img_convert_astc.py gta3.dir gta3.img out.dir out.img [--gles 1] [-j N]` |
| `img_rebuild.py` | 用替换文件重建归档，条目允许变大，全部按 2048B 扇区对齐、顺序重排偏移 |
| `img_patch.py` | 原地补丁，替换文件不能超过原槽位扇区数，输出新的 .dir/.img 对 |
| `txd_compress.py` | 单个 TXD 转换器（`in.txd out.txd [--astc] [--gles 0\|1]`），被上面的整包工具调用 |

### 按条目序号寻址，不按名字

`gta3.dir` 里有 7 个 TXD 名字各出现两次（共 14 个条目：chef、Pga、Pgb、camera、metal、Generic、airstuff2），另有 4 个 DFF 同名。所有工具内部都以 **gta3.dir 条目序号（idx）** 为键，名字只用来查找。

`img_rebuild.py` / `img_patch.py` 的替换规格有两种：

- `name=file`：只有名字恰好匹配一个条目时可用。名字有歧义时以退出码 2 退出，并列出候选 idx。
- `idx:N=file`：按目录条目序号（从 0 起）指定。不要写 `#N`，shell 会把它当注释。

```bash
# 名字唯一，直接用名字
python3 tools/r36s/img_rebuild.py gta3.dir gta3.img out.dir out.img IGmike.txd=new_mike.txd
# 同名条目，按 idx 指定（<N1>/<N2> 换成真实序号；先用 name=file 试一次，退出码 2 会列出候选 idx）
python3 tools/r36s/img_rebuild.py gta3.dir gta3.img out.dir out.img idx:<N1>=new_chef_a.txd idx:<N2>=new_chef_b.txd
```

### img_dup_check.py：同名条目回归检查（只读）

```bash
python3 tools/r36s/img_dup_check.py --src-dir S.dir --src-img S.img --out-dir O.dir --out-img O.img \
    [--full] [--refs --ide FILE [--ide FILE ...]] [--preserve B.dir B.img]
```

- 默认快速模式：只 seek 读取同名组的槽位。`--full` 检查所有 TXD 条目的贴图名（会读完整个归档）。
- 检查项：结构（条目数、名字顺序、偏移连续、.img 长度）、同名组报告、同名组身份（源里相同/不同，输出里也必须相同/不同，用来发现被按名字合并的条目）、贴图名一致。
- `--refs --ide FILE`：IDE 里模型的 DFF 引用的贴图，在源 TXD 中存在的必须也在输出 TXD 中存在；源里本来就缺的只提示。
- `--preserve B.dir B.img`：与基准归档比较，非同名组 TXD 的条目必须大小和字节都相同。
- `--out-prefix-skip N` 仅供测试用的假数据（假转换器会在输出前加标记），真实归档不要用。
- 退出码：0 全部通过，1 有检查失败，2 用法或结构错误（文件缺失、条目数/名字不一致、偏移不连续、.img 长度不对）。

### img_dup_fix.py：增量修复已转换的归档

旧版按名字转换的工具会让同名 TXD 的两个槽位写入同一份内容。本工具不需要整包重转：以已转换的归档为 BASE，从原始归档 SOURCE 里只重新转换同名 TXD，其余条目从 BASE 原样复制。

```bash
python3 tools/r36s/img_dup_fix.py --base-dir B.dir --base-img B.img \
    --src-dir S.dir --src-img S.img --out-dir O.dir --out-img O.img \
    [--gles 1] [-j N] [--tools-dir DIR] [--spot-check N]
```

- `--spot-check N`：写出之前，先把 SOURCE 里 N 个名字唯一的 TXD 再转一遍，必须与 BASE 同 idx 的槽位一致，否则说明 BASE 不是同一流水线产物（退出码 1）。
- 输出先写成 `.partial`，所有自检通过后才重命名为最终文件；失败时不会留下最终输出。
- 需要 `astcenc` 和 PIL（`txd_compress.py` 依赖），`--tools-dir` 目录里要有 `txd_compress.py`。
- 退出码：0 成功，1 检查或转换失败，2 用法或输入不匹配（BASE 与 SOURCE 的条目数或名字不一致）。

### 运行测试

```bash
python3 tools/r36s/tests/test_img_dup.py                         # 全部
python3 tools/r36s/tests/test_img_dup.py BugConditionExploration # 只跑某个测试类
```

测试只用标准库，转换器由假的 `txd_compress.py` 驱动，不需要 astcenc / PIL。`tests/fixtures/baseline_unfixed.json` 是在未修复代码上记录的基线，**不要重新录制**。
