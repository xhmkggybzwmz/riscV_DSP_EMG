#ifndef __SPI_H__
#define __SPI_H__
#ifdef __cplusplus
extern "C" {
#endif
#include "main.h"
extern DMA_HandleTypeDef hdma_spi3_tx;
void MX_SPI3_Init(void);
HAL_StatusTypeDef EMG_SPI3_Transmit_DMA(uint8_t *data, uint16_t size);
HAL_StatusTypeDef EMG_SPI3_Abort(void);
void EMG_SPI3_TxCpltCallback(void);
void EMG_SPI3_ErrorCallback(void);
#ifdef __cplusplus
}
#endif
#endif
