/* mbx_dma_r18.c - E5a simulation image: as mbx_dma_live, stops after 18 results. */
#define DMA_STOP_RESULTS 18u
#include "dma_common.h"

int main(void)
{
    return dma_run();
}
