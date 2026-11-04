/* c_crc16.c - CRC-16/CCITT-FALSE as used by the STM32 EMG1 protocol
 * (poly 0x1021, init 0xFFFF, no reflection, xorout 0; same loop as
 * firmware/stm32 EMG_SendBlock and host/emg_capture/emg_host.py).
 *
 * Expected values:
 *   "123456789"              -> 0x29B1 (published check value of CRC-16/CCITT-FALSE)
 *   EMG1 packet below        -> 0x6501 (computed with crc16_ccitt_false() of
 *                               host/emg_capture/emg_host.py)
 * The EMG1 packet is built here field by field like EMG_SendBlock: magic,
 * version 1, format U16_LE, 1 channel, reserved, 4000 Hz, first sample 256,
 * 8 samples per channel, 16 payload bytes, then the samples (little endian).
 */
#include <stdint.h>
#include "rv32_soc.h"

static uint16_t crc16_update(uint16_t crc, const uint8_t *data, uint32_t length)
{
    for (uint32_t i = 0; i < length; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (uint8_t bit = 0; bit < 8; bit++) {
            if (crc & 0x8000)
                crc = (uint16_t)((crc << 1) ^ 0x1021);
            else
                crc <<= 1;
        }
    }
    return crc;
}

static void put_u16_le(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put_u32_le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

static const uint16_t samples[8] = {2048, 2100, 1990, 4095, 0, 1234, 3000, 2047};
static const uint8_t check_str[9] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
static uint8_t packet[20 + 16];

int main(void)
{
    CHECK(crc16_update(0xFFFF, check_str, 9) == 0x29B1, 1);

    packet[0] = 'E'; packet[1] = 'M'; packet[2] = 'G'; packet[3] = '1';
    packet[4] = 1;          /* version */
    packet[5] = 0x01;       /* format U16_LE */
    packet[6] = 1;          /* channels */
    packet[7] = 0;          /* reserved */
    put_u32_le(&packet[8], 4000);
    put_u32_le(&packet[12], 256);
    put_u16_le(&packet[16], 8);
    put_u16_le(&packet[18], 16);
    for (int i = 0; i < 8; i++)
        put_u16_le(&packet[20 + 2 * i], samples[i]);
    CHECK(packet[8] == 0xA0 && packet[9] == 0x0F, 2);           /* 4000 little endian */

    uint16_t crc = crc16_update(0xFFFF, packet, 20);              /* header ... */
    crc = crc16_update(crc, &packet[20], 16);                     /* ... then payload */
    CHECK(crc == 0x6501, 3);
    CHECK(crc16_update(0xFFFF, packet, 36) == 0x6501, 4);         /* one pass, same result */
    return 0;
}
