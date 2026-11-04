#include "spi.h"

DMA_HandleTypeDef hdma_spi3_tx;
static volatile uint8_t spi3_dma_active;

static void SPI3_DMATxCplt(DMA_HandleTypeDef *hdma)
{
  (void)hdma;
  CLEAR_BIT(SPI3->CR2, SPI_CR2_TXDMAEN);
  while ((SPI3->SR & SPI_SR_BSY) != 0U) {}
  spi3_dma_active = 0;
  EMG_SPI3_TxCpltCallback();
}

static void SPI3_DMAError(DMA_HandleTypeDef *hdma)
{
  (void)hdma;
  CLEAR_BIT(SPI3->CR2, SPI_CR2_TXDMAEN);
  spi3_dma_active = 0;
  EMG_SPI3_ErrorCallback();
}

void MX_SPI3_Init(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};
  __HAL_RCC_SPI3_CLK_ENABLE();
  __HAL_RCC_GPIOC_CLK_ENABLE();
  GPIO_InitStruct.Pin = GPIO_PIN_10 | GPIO_PIN_12;
  GPIO_InitStruct.Mode = GPIO_MODE_AF_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
  GPIO_InitStruct.Alternate = GPIO_AF6_SPI3;
  HAL_GPIO_Init(GPIOC, &GPIO_InitStruct);

  hdma_spi3_tx.Instance = DMA1_Stream5;
  hdma_spi3_tx.Init.Channel = DMA_CHANNEL_0;
  hdma_spi3_tx.Init.Direction = DMA_MEMORY_TO_PERIPH;
  hdma_spi3_tx.Init.PeriphInc = DMA_PINC_DISABLE;
  hdma_spi3_tx.Init.MemInc = DMA_MINC_ENABLE;
  hdma_spi3_tx.Init.PeriphDataAlignment = DMA_PDATAALIGN_BYTE;
  hdma_spi3_tx.Init.MemDataAlignment = DMA_MDATAALIGN_BYTE;
  hdma_spi3_tx.Init.Mode = DMA_NORMAL;
  hdma_spi3_tx.Init.Priority = DMA_PRIORITY_HIGH;
  hdma_spi3_tx.Init.FIFOMode = DMA_FIFOMODE_DISABLE;
  if (HAL_DMA_Init(&hdma_spi3_tx) != HAL_OK) Error_Handler();

  CLEAR_BIT(SPI3->I2SCFGR, SPI_I2SCFGR_I2SMOD);
  SPI3->CR2 = 0;
  SPI3->CR1 = SPI_CR1_MSTR | SPI_CR1_BR_1 | SPI_CR1_SSM | SPI_CR1_SSI |
              SPI_CR1_BIDIMODE | SPI_CR1_BIDIOE;
  SET_BIT(SPI3->CR1, SPI_CR1_SPE);
}

HAL_StatusTypeDef EMG_SPI3_Transmit_DMA(uint8_t *data, uint16_t size)
{
  if (spi3_dma_active || data == 0 || size == 0) return HAL_BUSY;
  spi3_dma_active = 1;
  hdma_spi3_tx.XferCpltCallback = SPI3_DMATxCplt;
  hdma_spi3_tx.XferHalfCpltCallback = 0;
  hdma_spi3_tx.XferErrorCallback = SPI3_DMAError;
  if (HAL_DMA_Start_IT(&hdma_spi3_tx, (uint32_t)data, (uint32_t)&SPI3->DR, size) != HAL_OK) {
    spi3_dma_active = 0;
    return HAL_ERROR;
  }
  SET_BIT(SPI3->CR2, SPI_CR2_TXDMAEN);
  return HAL_OK;
}

HAL_StatusTypeDef EMG_SPI3_Abort(void)
{
  CLEAR_BIT(SPI3->CR2, SPI_CR2_TXDMAEN);
  spi3_dma_active = 0;
  return HAL_DMA_Abort(&hdma_spi3_tx);
}
