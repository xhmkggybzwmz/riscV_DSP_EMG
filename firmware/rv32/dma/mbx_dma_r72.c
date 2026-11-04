/* mbx_dma_r72.c - E5a simulation image: as mbx_dma_live, stops after 72 results. */
#define DMA_STOP_RESULTS 72u
#include "dma_common.h"

int main(void)
{
    return dma_run();
}
