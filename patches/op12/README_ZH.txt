OnePlus 12 / CPH2581_16.0.0.201(EX01) / 6.1.118 HMBIRD 构建修复

源码与补丁

主源码仍是 OnePlus 官方 .201 的真实 6.1.118：
  common: 8545ca9de03997a9411c35e9d15f2aeed4301710
  msm-kernel: 98f9773760f4f83ecd74be14e63f6919e4b5115c
  modules/devicetree: 100829f4f16ff59ea43b6a0cf5f9b9f03b8794c6

没有把 6.1.141 改名成 6.1.118，也没有把整个内核降到 6.1.75。
6.1.118-hmbird-compat.patch 只回移调度器相关改动，根 Makefile 不在补丁中。

HMBIRD 核心来源是 Realme 官方仓库的早期公开提交：
  https://github.com/realme-kernel-opensource/realme_GT5pro-AndroidV-common-source
  HMBIRD: 920a71d0f589dac6bd56898ae2261d593df08d97
  对照:   802d968fb2c726f0a9dd88fed80a003d724769d4

这个参考实现本身基于 6.1.75。本补丁将其调度器改动合并到 .201 的 ACK 6.1.118 基础上，
保留 6.1.118 上游内容，并补齐从本机原厂二进制确认的接口及布局：
  task_struct.scx 位于字节偏移 3608；sched_ext_entity 为 264 字节；
  top_task_prop、dsq_sync_ux 和完整 scx_iso_masks 布局；
  真实隔离 CPU 掩码的导出及原厂 UX tracepoint。

这是一份经过本地编译和静态对照的回移实现，不是原厂私有源码的逐字节复原。
scx_get_md_info 返回本实现的实时调度统计区，用于厂商 minidump 收集；
它不复刻原厂私有 4096 字节日志缓冲区的内容格式。
游戏调度策略、功耗及性能是否与原厂一致，仍需后续独立的运行验证。

需要一起保留的文件

  .github/workflows/build-op12-61118.yml
  scripts/apply_op12_hmbird.py
  scripts/check_op12_kernel_compat.py
  scripts/compat/op12-oos16.0.0.201.json
  patches/op12/6.1.118-hmbird-compat.patch
  patches/op12/hmbird-source-lock.json
  patches/op12/README_ZH.txt
  tests/test_op12_compat.py
  tests/fixtures/op12-incompatible-slim.json

不要只上传一个 yml：工作流会使用同一次 checkout 中的脚本、补丁和原厂兼容性基准。
不再重新拉取 fork 的最新分支来取得这些辅助文件，避免运行提交与辅助脚本不一致。

SPACE_NOCLEAN=false 时，空间清理脚本会把新卷挂载到整个 GITHUB_WORKSPACE，
遮住首次 checkout 的全部文件。工作流在挂载后按 github.sha 再次 checkout，
确认 HEAD 与本次运行提交一致后，才归档辅助文件并记录 workflow-commit.txt。
SPACE_NOCLEAN=true 时跳过挂载及第二次 checkout，直接使用首次 checkout。
修改并推送 workflow 后应通过 Run workflow 发起新运行；旧运行的 Re-run jobs
仍使用旧的提交与工作流，不会带上这次修复。

检查与阻断规则

1. 源码固定提交必须一致，两个源码 Makefile 必须真实为 6.1.118，KMI 代次必须为 11。
   RESUBLEVEL 必须留空，禁止通过改版本数字绕过检查。
2. 应用补丁前检查 SHA256 和可应用性；应用后检查受影响文件 SHA256。
   检查不会忽略冲突，不会修改根 Makefile。
3. olddefconfig 后确认内置 SukiSU / SUSFS / KPM、HMBIRD、MODVERSIONS、BTF 及
   BTF_MODULES 等关键配置保留。SLIM_SCHED / SCHED_CLASS_EXT 不允许启用。
4. KPM 后处理之后检查最终 AnyKernel3/Image：
   - release 必须仍属 6.1.118-android14；
   - 必须有真实嵌入配置；
   - 8079 个原厂导出不能缺失，CRC 不能改变；新增导出允许；
   - task_struct、rq、task_group、sched_ext_entity、scx_dispatch_q、
     scx_sched_task_stats、scx_iso_masks 的原有字段偏移和大小必须匹配。
5. 任一关键检查失败，AnyKernel3 上传步骤不会执行。
   审计上传在失败时仍尝试运行，以便取得具体原因。

原厂兼容性 JSON 是从当前可启动 boot_a 导出的内核提取的公开接口元数据，
包括原厂 Image SHA256、导出 CRC 和选定结构布局，不包含设备镜像、驱动二进制或日志。
无需将 diagnostics/ 上传到 Git。

建议构建输入

  FILE=oneplus_12_b
  FAST_BUILD=true
  RESUBLEVEL=留空
  KPM=KPM
  KSU_META=main/builtin/OP12-61118/1cfa57340590971cba7521ed536da8ebbedc5f0a
  SUSFS_META=a2165b6f875b73e2f87d3c6f12814efac8de061d
  SUSFS_CI=N/A
  SCHED_HMBIRD=false
  LSM_BBG=false
  NETFILTER=false
  CCM=false
  ZRAM=0/lz4kd/8589934592
  LZ4_UPDATE=false
  DROID_SPACES=false
  RE_KERNEL=false

SCHED_HMBIRD 指原版 workflow 的通用风驰补丁选项。本配方强制它为 false，
专用 OP12 HMBIRD 回移始终执行，并在最终 Image 中检查 CONFIG_HMBIRD_SCHED=y。
因此这里的 false 不表示关闭 HMBIRD。

BBG、网络扩展、BBR 等可选项保留，但默认关闭，以便先确认核心构建。
后续启用的选项若改变原厂 ABI，仍会被最终检查拦截，不能删除检查来绕过。

SukiSU builtin 和 SUSFS 保持原先固定提交；KPM 补丁工具及 AnyKernel3 也固定到具体提交。
管理器版本标签固定为 v4.3.0-r2，不再把最新发布标签写入旧驱动版本信息。
其他原版 workflow 的可选远程依赖不代表全部供应链都已固定。

本地检查示例

仅检查本地文件，不连接或刷写手机：

  python3 scripts/check_op12_kernel_compat.py \
    --reference-json scripts/compat/op12-oos16.0.0.201.json \
    --candidate /实际路径/Image \
    --output /实际路径/kernel-compatibility.json

也可将 --candidate 指向 AnyKernel3 ZIP 或 boot header v3/v4 的 boot.img。
如果有已提取的原厂 .ko，可增加 --module-dir /实际路径/原厂模块目录。
JSON 基准生成时使用 --reference 原厂镜像 --save-reference 输出路径；
更新基准后必须重新审阅并更新 source-lock 中的哈希，不能拿自编内核替代原厂基准。

验证范围

本地隔离 Linux 编译用于验证 HMBIRD 回移；使用原生 arm64 Clang 18.1.3。
本地完整基础内核检查已通过：8079 个原厂导出无缺失、CRC 无变化，7 个调度结构的
检查无差异，对 472 个原厂 .ko 未发现内核依赖缺失或 CRC 冲突。
旧失败包仍被拒绝：32 个缺失导出、5 个 CRC 变化、13 项所检查布局差异。
5 项自动回归测试和 4 项源码应用/版本保护测试通过；49 个 workflow Bash 段语法通过。
详细机器可读记录见 local-validation.json。
正式 Actions 继续使用固定源码的 Android Clang r487747c / 17.0.2。
完整内置 SukiSU / SUSFS / KPM 构建及 KPM 后处理结果，以新的 Actions 审计产物为准。
静态 ABI/BTF 检查不覆盖全部运行时行为，也不等于手机启动、SUSFS 隐藏或 KPM 模块测试。
本次未刷写分区、未重启手机、未切换槽位，也未创建 git commit。
