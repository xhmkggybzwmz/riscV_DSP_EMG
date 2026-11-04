/* link.ld - minimal bare-metal layout for RV32I core tests.
 *
 * Everything (code, read-only data, data, bss) lives in the 64 KB local RAM
 * at 0x0000_0000 (docs/soc/memory_map.md). The reset vector is 0, so crt0's
 * .text.init must come first. No data copy is needed: the program image is
 * loaded into RAM as a whole. The stack starts at the top of RAM.
 */
OUTPUT_ARCH("riscv")
ENTRY(_start)

MEMORY
{
    RAM (rwx) : ORIGIN = 0x00000000, LENGTH = 64K
}

SECTIONS
{
    .text : {
        KEEP(*(.text.init))
        *(.text .text.*)
    } > RAM

    .rodata : ALIGN(4) {
        *(.rodata .rodata.* .srodata .srodata.*)
    } > RAM

    .data : ALIGN(4) {
        *(.data .data.* .sdata .sdata.*)
    } > RAM

    .bss (NOLOAD) : ALIGN(4) {
        __bss_start = .;
        *(.bss .bss.* .sbss .sbss.* COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > RAM

    __stack_top = ORIGIN(RAM) + LENGTH(RAM);

    /* the Vivado-bundled toolchain emits a build-id note by default; it must not
       be placed at the reset vector */
    /DISCARD/ : { *(.comment) *(.note.gnu.build-id) }
}
