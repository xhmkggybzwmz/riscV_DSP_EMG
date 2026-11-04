"""Paper budget for the E3 hardware (NOT a synthesis result): storage, MACs, cycles, margins.

Model of a time-multiplexed CNN engine with P parallel MAC lanes (P output channels at once):
  - every output element needs ceil(C_out / P) passes; a pass takes K*C_in MAC cycles plus a fixed
    per-pass overhead OV (pipeline fill/drain, bias load, requant, write-back) - OV is an assumption
  - weights: one int8 per MAC per lane read from on-chip RAM (P bytes / cycle); activations: one
    value per MAC shared by the P lanes (broadcast)
  - DSP front end: one biquad engine time-multiplexed over lanes x sections, 5 MAC + OV_DSP per section
Worst case per decision: the 100 input samples of one step trigger 50 conv1, 25 conv2 and 5 conv3
outputs plus one FC; the conv3 output completing the window waits for its conv2/conv1 inputs.
"""

from __future__ import annotations

import math


def budget(n_ch=4, layers=((16, 8, 2), (32, 7, 2), (32, 6, 5)), n_cls=6, fs=2000.0, f_clk=100e6, window=400,
           step=100, P_list=(1, 2, 4, 8, 16), OV=8, OV_DSP=4, dsp_sections=3, M_gap=19) -> dict:
    out = {"assumptions": {"f_clk_hz": f_clk, "fs_hz": fs, "per_pass_overhead_cycles": OV,
                           "dsp_section_overhead_cycles": OV_DSP, "n_ch": n_ch,
                           "note": "estimates only; no RTL, no synthesis"}}
    c_in, prod, store, lines = n_ch, 1, [], []
    per_step = []                       # (outputs per 100-sample step, C_out, MAC per output element)
    for i, (c_out, k, s) in enumerate(layers):
        prod *= s
        w_bytes = c_out * c_in * k
        store.append({"layer": f"conv{i + 1}", "weights_int8_bytes": w_bytes, "bias_int32_bytes": 4 * c_out,
                      "requant_bytes": 3 * c_out})
        in_bits = 16 if i == 0 else 8
        lines.append({"layer": f"conv{i + 1}", "line_buffer_bits": k * c_in * in_bits,
                      "note": f"last {k} input steps x {c_in} channels x {in_bits} bit"})
        per_step.append((step // prod, c_out, k * c_in))
        c_in = c_out
    fc_w = n_cls * c_in
    store.append({"layer": "fc", "weights_int8_bytes": fc_w, "bias_int32_bytes": 4 * n_cls, "requant_bytes": 0})
    gap_bits = M_gap * c_in * 8 + c_in * 13
    lines.append({"layer": "gap", "line_buffer_bits": gap_bits,
                  "note": f"ring of {M_gap} conv3 outputs x {c_in} x 8 bit + {c_in} 13-bit running sums"})
    dsp_state_bits = n_ch * dsp_sections * 4 * 27
    lines.append({"layer": "dsp", "line_buffer_bits": dsp_state_bits,
                  "note": f"{n_ch} lanes x {dsp_sections} sections x 4 state words x 27 bit"})
    tot_w = sum(s["weights_int8_bytes"] for s in store)
    tot_p = sum(s["bias_int32_bytes"] + s["requant_bytes"] for s in store)
    out["storage"] = {"per_layer": store, "weights_bytes": tot_w, "bias_and_requant_bytes": tot_p,
                      "line_buffers": lines, "line_buffer_bits_total": sum(l["line_buffer_bits"] for l in lines),
                      "bram36_for_weights": math.ceil((tot_w + tot_p) * 8 / 36864),
                      "dsp_coefficient_bits": dsp_sections * 5 * 18}
    macs_step = sum(n * c * m for n, c, m in per_step) + fc_w
    out["macs"] = {"per_100_sample_step": macs_step, "per_second": macs_step * fs / step,
                   "dsp_front_end_per_second": n_ch * dsp_sections * 5 * fs}
    rows = []
    for P in P_list:
        cyc = sum(n * math.ceil(c / P) * (m + OV) for n, c, m in per_step) + math.ceil(n_cls / P) * (c_in + OV)
        dsp_cyc = int(step * n_ch * dsp_sections * (5 + OV_DSP))
        total = cyc + dsp_cyc
        # worst-case latency after the last sample of a step: the last conv1 output (2 samples) ->
        # its conv2 output -> the conv3 output -> FC, sequential through one engine
        chain = sum(math.ceil(c / P) * (m + OV) for _, c, m in per_step) + math.ceil(n_cls / P) * (c_in + OV)
        avail = f_clk * step / fs
        rows.append({"P_mac_lanes": P, "dsp48_cnn": P, "cycles_per_step_cnn": cyc, "cycles_per_step_dsp": dsp_cyc,
                     "cycles_available_per_step": int(avail), "utilisation": total / avail,
                     "decision_latency_cycles_after_last_sample": chain + n_ch * dsp_sections * (5 + OV_DSP),
                     "decision_latency_us": (chain + n_ch * dsp_sections * (5 + OV_DSP)) / f_clk * 1e6,
                     "weight_read_bytes_per_cycle": P, "activation_reads_per_cycle": 1})
    out["throughput"] = rows
    out["valid_ready"] = ("ESI frames arrive at 2 kHz (one every 50 000 cycles at 100 MHz). The core may hold tready "
                          "low while computing; the ESI source FIFO (64 frames) absorbs it. Requirement: total work "
                          "per step < available cycles with margin, and burst latency < one frame period "
                          "(500 us) so the FIFO never grows.")
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(budget(), indent=1))
