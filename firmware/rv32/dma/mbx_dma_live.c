/* mbx_dma_live.c - E5a board image: configures DSP and CNN, publishes every result, never stops. */
#include "dma_common.h"

int main(void)
{
    return dma_run();
}
