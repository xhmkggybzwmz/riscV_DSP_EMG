"""DSP-R0: float reference of the future FPGA DSP accelerator (causal, stream semantics)."""

from .config import DSPConfig, STAGE_ORDER
from .pipeline import EMGPreprocessPipeline, PipelineOutput

__all__ = ["DSPConfig", "STAGE_ORDER", "EMGPreprocessPipeline", "PipelineOutput"]
