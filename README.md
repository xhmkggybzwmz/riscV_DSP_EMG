# 面向实时肌电手势识别的自研 RISC-V 异构 SoC 与 FPGA 加速系统

STM32 采集肌电 → SPI → KV260 PL（自研 RV32I + 定点 DSP + INT8 CNN）→ ARM PS → WebSocket → PC 三维手。设计和结果见 [technical_document.docx](technical_document.docx)。

| 通路 | 比特流变体 | 输入与模型 |
|---|---|---|
| SPI | `ps_e6a` | STM32 经 SPI 送入单通道肌电，放松、轻握、用力三档 |
| DMA | `ps_e5a` | PS 回放 GRABMyo 4 通道记录，六类手势 |

## 环境

Windows 11，Python 3.12（`pip install -r requirements.txt`），Git，Icarus Verilog 12，Vivado 2026.1，STM32CubeIDE 1.19。KV260 需要 Ubuntu + PYNQ 和 SSH 登录。

工具不在默认位置时，设置 `RISCV_EMG_VIVADO_ROOT`（指向 `...\2026.1\Vivado`）、`RISCV_EMG_ICARUS_BIN`、`RISCV_EMG_CUBEIDE`。请把仓库放在较短的路径下，并保持工作区干净。

```bash
python scripts/dev/check_env.py
```

## 仿真

```bash
python scripts/sim/run_spi.py
```

```bash
python scripts/sim/run_spi.py --subsys
```

## 构建

比特流，输出在 `build/vivado/kv260_e6a/mbx_dma_live/out/`：

```bash
python scripts/sim/gen_spi_live.py --model analysis/spi/model
```

```bash
python scripts/fpga/kv260_6a.py build mbx_dma_live --variant ps_e6a --jobs 6
```

```bash
python scripts/fpga/kv260_6a.py manifest mbx_dma_live --variant ps_e6a
```

STM32 固件，输出在 `build/stm32/`（`test` 计数器，`adc` 传感器经 SPI，`adc_uart` 传感器经串口）：

```bash
python scripts/sw/build_stm32.py
```

## 上板

| STM32 | PC10 | PC12 | PA4 | PA6 | GND | PA5 |
|---|---|---|---|---|---|---|
| 连接到 | J2.1 | J2.3 | J2.5 | J2.7 | J2.9 | MyoWare RAW，经 10 kΩ / 10 kΩ 分压 |

> **安全：** 电极贴在人身上时，电脑用电池供电，不要连接 KV260。
>
> **不要打开 JTAG**（Vivado Hardware Manager、XSDB）：板上 Linux 会挂死。只用下面的脚本加载；失败不要自动重试。

在 PC 上复制文件：

```bash
B=ubuntu@<板子 IP>; D=riscv-emg-soc/e6a; O=build/vivado/kv260_e6a/mbx_dma_live/out
"$RISCV_EMG_VIVADO_ROOT/bin/dtc" -@ -I dts -O dtb -o build/rv32_6a.dtbo fpga/kv260/dts/rv32_6a.dts
ssh $B "mkdir -p $D/fw $D/overlays/mbx_dma_live $D/web $D/logs"
scp scripts/fpga/kv260_ps_board.py analysis/spi/model/levels.json $B:$D/
scp build/rv32_6a.dtbo $B:$D/fw/rv32_6a.dtbo
scp $O/kv260_soc.bit.bin $B:$D/fw/mbx_dma_live.bit.bin
scp $O/kv260_soc.bit $B:$D/overlays/mbx_dma_live/mbx_dma_live.bit
scp $O/kv260_soc.hwh $B:$D/overlays/mbx_dma_live/mbx_dma_live.hwh
scp $O/manifest.json $B:$D/overlays/mbx_dma_live/mbx_dma_live.manifest.json
(cd host && tar --exclude=__pycache__ --exclude=tests -czf - spi_monitor spi_live ps_mailbox gesture_server) | ssh $B "cd $D && tar xzf -"
(cd visualization/web && tar --exclude=node_modules --exclude=test -czf - .) | ssh $B "cd $D/web && tar xzf -"
```

在板子上加载并启动，然后用 PC 浏览器打开 `http://<板子 IP>:8765/`：

```bash
cd ~/riscv-emg-soc/e6a && sudo python3 kv260_ps_board.py check > check_before.txt && sudo xmutil unloadapp && sudo python3 kv260_ps_board.py load mbx_dma_live.bit.bin
```

```bash
sudo env XILINX_XRT=/usr PATH=/usr/local/share/pynq-venv/bin:$PATH /usr/local/share/pynq-venv/bin/python3 -B -m gesture_server.cli --source spi_live --bit overlays/mbx_dma_live/mbx_dma_live.bit --loaded-bin fw/rv32_6a.bit.bin --levels levels.json --policy live --web web --host <板子 IP> --lan --jsonl logs/run.jsonl
```

结束后恢复板子：

```bash
sudo pkill -TERM -f '[g]esture_server.cli'; sudo python3 kv260_ps_board.py unload && sudo xmutil loadapp k26-starter-kits
```

## 数据与模型

仓库含三档模型权重（`analysis/spi/model/`），不含录制数据和比特流，所以报告中的三档准确率和 6317 条比对不能只用本仓库重算。可以用自己的数据走同样的流程：

```bash
python host/spi_live/capture_uart.py --port <串口> --out analysis/spi/rec1.npz --rounds 6 --hold 10
```

```bash
python ml/scripts/train_spi_levels.py --rec analysis/spi/rec1.npz analysis/spi/rec2.npz --out analysis/spi/my_model
```

六类手势需要下载 GRABMyo G1（约 800 MB）并训练：

```bash
python ml/scripts/fetch_grabmyo.py download --subset G1
```

```bash
python ml/scripts/run_e1b.py --config ml/configs/e1b_g1.toml --out "<仓库绝对路径>/analysis/ml/e1b/e1b_g1_20261020_035336"
```

```bash
python ml/scripts/run_e2.py --config ml/configs/e2_g1.toml --out "<仓库绝对路径>/analysis/ml/e2/e2_20261022_023953"
```

## 许可

本项目的代码使用 [MIT 许可证](LICENSE)。以下第三方内容保留各自的许可：`firmware/stm32/emg_acquisition/Drivers/`（STMicroelectronics、Arm，见其中的 LICENSE.txt）、`visualization/web/vendor/three.module.js`（Three.js，MIT）、`reference/` 中由 GRABMyo 派生的测试向量（GRABMyo 1.1.0，CC BY 4.0）。
