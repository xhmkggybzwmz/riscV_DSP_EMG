/* mbx_dma_r36.c - E5a simulation image: as mbx_dma_live, stops after 36 results. */
#define DMA_STOP_RESULTS 36u
#include "dma_common.h"

int main(void)
{
    return dma_run();
}
