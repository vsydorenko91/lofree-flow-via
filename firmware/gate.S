/* OE915 hook gates. They live inside original zero padding. After every (re)connect the
 * stock handlers run for a short startup window (32 passes, so GPIO scanning has settled);
 * afterwards the VIA/tap-hold code takes over. While the gate state is not "active" every
 * hook falls through to the stock routine, so the stock update path always works. */
.syntax unified
.cpu arm7tdmi
.thumb
.section .th_gate,"ax",%progbits
.balign 4
.global th_gate_matrix
.thumb_func
th_gate_matrix:
    ldr r3, =th_gate_state
    ldrb r2, [r3]
    cmp r2, #0
    bne 3f
    /* Do not arm before GPIO scanning has completed a full row cycle.
     * Hold a 32-processing-pass startup window for debouncing to settle. */
    ldr r0, =0x4018d1
    ldrb r0, [r0]
    cmp r0, #0
    beq 4f
    ldr r3, =th_gate_state
    ldrb r2, [r3, #1]
    adds r2, #1
    strb r2, [r3, #1]
    cmp r2, #32
    blo 4f
    movs r2, #1
    strb r2, [r3]
3:  cmp r2, #1
    beq 5f
4:  ldr r0, =0x401aec
    ldr r1, =th_shadow
    movs r2, #6
6:  ldmia r0!, {r3}
    stmia r1!, {r3}
    subs r2, #1
    bne 6b
    ldr r3, =0x1beb5
    bx r3
5:  ldr r3, =th_matrix_process
    bx r3

.global th_gate_activity
.thumb_func
th_gate_activity:
    ldr r3, =th_gate_state
    ldrb r3, [r3]
    cmp r3, #1
    bne 1f
    ldr r3, =th_activity
    bx r3
1:  ldr r3, =0x166f1
    bx r3

.global th_gate_event
.thumb_func
th_gate_event:
    ldr r3, =th_gate_state
    ldrb r3, [r3]
    cmp r3, #1
    bne 1f
    ldr r3, =th_key_event
    bx r3
1:  ldr r3, =0x19899
    bx r3

.global th_gate_usb
.thumb_func
th_gate_usb:
    ldr r3, =th_gate_state
    ldrb r3, [r3]
    cmp r3, #1
    bne 1f
    ldr r3, =th_usb_submit
    bx r3
1:  ldr r3, =0x121ed
    bx r3

/* USB reset/reconnect re-arms the startup window even if the battery keeps SRAM alive
 * while the physical switch is OFF. This runs in the stock scan IRQ. */
.global th_gate_scan
.thumb_func
th_gate_scan:
    ldr r3, =0x4003c4
    ldrb r3, [r3]
    cmp r3, #0
    bne 1f
    ldr r3, =th_gate_state
    ldrb r1, [r3]
    cmp r1, #0
    beq 1f
    movs r2, #0
    strb r2, [r3]
    strb r2, [r3, #1]
    ldrh r1, [r3, #2]
    adds r1, #1
    strh r1, [r3, #2]
1:  ldr r3, =0x1b489
    bx r3



.global via_gate_ep0
.thumb_func
via_gate_ep0:
    ldr r3, =th_gate_state
    ldrb r3, [r3]
    cmp r3, #1
    bne 1f
    ldr r3, =via_ep0_complete
    bx r3
1:  ldr r3, =0x18249
    bx r3

.global via_gate_loader
.thumb_func
via_gate_loader:
    /* Seed the 19 factory macro records (228 B) of the RAM table from ROM so
     * stock macros also work in bypass. User records (>=228) are untouched. */
    ldr r0, =0x22b18
    ldr r1, =macro_table
    movs r2, #57
9:  ldmia r0!, {r3}
    stmia r1!, {r3}
    subs r2, #1
    bne 9b
    ldr r3, =th_gate_state
    ldrb r3, [r3]
    cmp r3, #1
    bne 1f
    ldr r3, =via_load_current
    bx r3
1:  ldr r3, =0x196cd
    bx r3

.global via_gate_poll
.thumb_func
via_gate_poll:
    ldr r3, =th_gate_state
    ldrb r3, [r3]
    cmp r3, #1
    bne 1f
    ldr r3, =via_poll
    bx r3
1:  bx lr

.ltorg

.section .bss.th_gate,"aw",%nobits
.balign 4
.global th_gate_state
th_gate_state:
.space 4
