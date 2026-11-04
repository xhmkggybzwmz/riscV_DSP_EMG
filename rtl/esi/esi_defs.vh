// esi_defs.vh - ESI v1-E3 field positions (docs/soc/esi_v1.md)
`ifndef ESI_DEFS_VH
`define ESI_DEFS_VH

`define ESI_TDATA_W   128
`define ESI_TUSER_W   80
`define ESI_NLANE     8

// tuser fields
`define ESI_FLAGS     15:0
`define ESI_SEQ       47:16
`define ESI_TS        79:48

// flag bits
`define ESI_F_SOS       0
`define ESI_F_GAP       1
`define ESI_F_OVR       2
`define ESI_F_UNDERRUN  3
`define ESI_F_POST_DSP  6

`endif
