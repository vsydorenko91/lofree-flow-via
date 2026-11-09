ENTRY(th_gate_matrix)
SECTIONS {
    . = 0x21c0;
    .th_gate : { *(.th_gate) }
    . = 0x24370;
    .text : { *(.text*) *(.rodata*) }
    .ARM.exidx : { *(.ARM.exidx*) }
    . = 0x404dc4;
    .bss (NOLOAD) : { *(.bss*) *(COMMON) }
    /DISCARD/ : { *(.comment) *(.note*) }
}
ASSERT(SIZEOF(.th_gate) <= 0x140, "bypass gate exceeds original zero padding")
ASSERT(ADDR(.bss) + SIZEOF(.bss) <= 0x406000, "VIA state exceeds bounded allocation below stock stacks")
ASSERT(ADDR(.text) + SIZEOF(.text) <= FLOW_MAX_CODE_SIZE, "unexpected executable growth")

ASSERT(ADDR(.ARM.exidx) + SIZEOF(.ARM.exidx) <= FLOW_MAX_CODE_SIZE, "appended image exceeds safety limit")
