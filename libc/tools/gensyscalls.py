#!/usr/bin/python
#
# this tool is used to generate the syscall assembler templates
# to be placed into arch-{arm,x86,mips}/syscalls, as well as the content
# of arch-{arm,x86,mips}/linux/_syscalls.h
#

import sys, os.path, glob, re, commands, filecmp, shutil
import getpass

from bionic_utils import *

# set this to 1 if you want to generate thumb stubs
gen_thumb_stubs = 0

# set this to 1 if you want to generate ARM EABI stubs
gen_eabi_stubs = 1

# get the root Bionic directory, simply this script's dirname
#
bionic_root = find_bionic_root()
if not bionic_root:
    print "could not find the Bionic root directory. aborting"
    sys.exit(1)

if bionic_root[-1] != '/':
    bionic_root += "/"

print "bionic_root is %s" % bionic_root

# temp directory where we store all intermediate files
bionic_temp = "/tmp/bionic_gensyscalls/"

# all architectures, update as you see fit
all_archs = [ "arm", "x86", "mips" ]

def make_dir( path ):
    path = os.path.abspath(path)
    if not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent:
            make_dir(parent)
        os.mkdir(path)

def create_file( relpath ):
    dir = os.path.dirname( bionic_temp + relpath )
    make_dir(dir)
    return open( bionic_temp + relpath, "w" )

# x86 assembler templates for each syscall stub
#

x86_header = """/* autogenerated by gensyscalls.py */
#include <sys/linux-syscalls.h>

    .text
    .type %(fname)s, @function
    .globl %(fname)s
    .align 4

%(fname)s:
"""

x86_registers = [ "%ebx", "%ecx", "%edx", "%esi", "%edi", "%ebp" ]

x86_call = """    movl    $%(idname)s, %%eax
    int     $0x80
    cmpl    $-129, %%eax
    jb      1f
    negl    %%eax
    pushl   %%eax
    call    __set_errno
    addl    $4, %%esp
    orl     $-1, %%eax
1:
"""

x86_return = """    ret
"""

# ARM assembler templates for each syscall stub
#
arm_header = """/* autogenerated by gensyscalls.py */
#include <machine/asm.h>
#include <sys/linux-syscalls.h>

ENTRY(%(fname)s)
"""

arm_footer = """\
END(%(fname)s)
"""

arm_call_default = arm_header + """\
    swi   #%(idname)s
    movs    r0, r0
    bxpl    lr
    b       __set_syscall_errno
""" + arm_footer

arm_call_long = arm_header + """\
    .save   {r4, r5, lr}
    stmfd   sp!, {r4, r5, lr}
    ldr     r4, [sp, #12]
    ldr     r5, [sp, #16]
    swi     # %(idname)s
    ldmfd   sp!, {r4, r5, lr}
    movs    r0, r0
    bxpl    lr
    b       __set_syscall_errno
""" + arm_footer

arm_eabi_call_default = arm_header + """\
    .save   {r4, r7}
    stmfd   sp!, {r4, r7}
    ldr     r7, =%(idname)s
    swi     #0
    ldmfd   sp!, {r4, r7}
    movs    r0, r0
    bxpl    lr
    b       __set_syscall_errno
""" + arm_footer

arm_eabi_call_long = arm_header + """\
    mov     ip, sp
    .save   {r4, r5, r6, r7}
    stmfd   sp!, {r4, r5, r6, r7}
    ldmfd   ip, {r4, r5, r6}
    ldr     r7, =%(idname)s
    swi     #0
    ldmfd   sp!, {r4, r5, r6, r7}
    movs    r0, r0
    bxpl    lr
    b       __set_syscall_errno
""" + arm_footer

# ARM thumb assembler templates for each syscall stub
#
thumb_header = """/* autogenerated by gensyscalls.py */
    .text
    .type %(fname)s, #function
    .globl %(fname)s
    .align 4
    .thumb_func
    .fnstart

#define  __thumb__
#include <sys/linux-syscalls.h>


%(fname)s:
"""

thumb_call_default = thumb_header + """\
    .save   {r7,lr}
    push    {r7,lr}
    ldr     r7, =%(idname)s
    swi     #0
    tst     r0, r0
    bmi     1f
    pop     {r7,pc}
1:
    neg     r0, r0
    ldr     r1, =__set_errno
    blx     r1
    pop     {r7,pc}
    .fnend
"""

thumb_call_long = thumb_header + """\
    .save  {r4,r5,r7,lr}
    push   {r4,r5,r7,lr}
    ldr    r4, [sp,#16]
    ldr    r5, [sp,#20]
    ldr    r7, =%(idname)s
    swi    #0
    tst    r0, r0
    bmi    1f
    pop    {r4,r5,r7,pc}
1:
    neg    r0, r0
    ldr    r1, =__set_errno
    blx    r1
    pop    {r4,r5,r7,pc}
    .fnend
"""

# mips assembler templates for each syscall stub
#
mips_call = """/* autogenerated by gensyscalls.py */
#include <sys/linux-syscalls.h>
    .text
    .globl %(fname)s
    .align 4
    .ent %(fname)s

%(fname)s:
    .set noreorder
    .cpload $t9
    li $v0, %(idname)s
    syscall
    bnez $a3, 1f
    move $a0, $v0
    j $ra
    nop
1:
    la $t9,__set_errno
    j $t9
    nop
    .set reorder
    .end %(fname)s
"""

def param_uses_64bits(param):
    """Returns True iff a syscall parameter description corresponds
       to a 64-bit type."""
    param = param.strip()
    # First, check that the param type begins with one of the known
    # 64-bit types.
    if not ( \
       param.startswith("int64_t") or param.startswith("uint64_t") or \
       param.startswith("loff_t") or param.startswith("off64_t") or \
       param.startswith("long long") or param.startswith("unsigned long long") or
       param.startswith("signed long long") ):
           return False

    # Second, check that there is no pointer type here
    if param.find("*") >= 0:
            return False

    # Ok
    return True

def count_arm_param_registers(params):
    """This function is used to count the number of register used
       to pass parameters when invoking a thumb or ARM system call.
       This is because the ARM EABI mandates that 64-bit quantities
       must be passed in an even+odd register pair. So, for example,
       something like:

             foo(int fd, off64_t pos)

       would actually need 4 registers:
             r0 -> int
             r1 -> unused
             r2-r3 -> pos
   """
    count = 0
    for param in params:
        if param_uses_64bits(param):
            if (count & 1) != 0:
                count += 1
            count += 2
        else:
            count += 1
    return count

def count_generic_param_registers(params):
    count = 0
    for param in params:
        if param_uses_64bits(param):
            count += 2
        else:
            count += 1
    return count

class State:
    def __init__(self):
        self.old_stubs = []
        self.new_stubs = []
        self.other_files = []
        self.syscalls = []

    def x86_genstub(self, fname, numparams, idname):
        t = { "fname"  : fname,
              "idname" : idname }

        result     = x86_header % t
        stack_bias = 4
        for r in range(numparams):
            result     += "    pushl   " + x86_registers[r] + "\n"
            stack_bias += 4

        for r in range(numparams):
            result += "    mov     %d(%%esp), %s" % (stack_bias+r*4, x86_registers[r]) + "\n"

        result += x86_call % t

        for r in range(numparams):
            result += "    popl    " + x86_registers[numparams-r-1] + "\n"

        result += x86_return
        return result

    def x86_genstub_cid(self, fname, numparams, idname, cid):
        # We'll ignore numparams here because in reality, if there is a
        # dispatch call (like a socketcall syscall) there are actually
        # only 2 arguments to the syscall and 2 regs we have to save:
        #   %ebx <--- Argument 1 - The call id of the needed vectored
        #                          syscall (socket, bind, recv, etc)
        #   %ecx <--- Argument 2 - Pointer to the rest of the arguments
        #                          from the original function called (socket())
        t = { "fname"  : fname,
              "idname" : idname }

        result = x86_header % t
        stack_bias = 4

        # save the regs we need
        result += "    pushl   %ebx" + "\n"
        stack_bias += 4
        result += "    pushl   %ecx" + "\n"
        stack_bias += 4

        # set the call id (%ebx)
        result += "    mov     $%d, %%ebx" % (cid) + "\n"

        # set the pointer to the rest of the args into %ecx
        result += "    mov     %esp, %ecx" + "\n"
        result += "    addl    $%d, %%ecx" % (stack_bias) + "\n"

        # now do the syscall code itself
        result += x86_call % t

        # now restore the saved regs
        result += "    popl    %ecx" + "\n"
        result += "    popl    %ebx" + "\n"

        # epilog
        result += x86_return
        return result

    def arm_genstub(self,fname, flags, idname):
        t = { "fname"  : fname,
              "idname" : idname }
        if flags:
            numargs = int(flags)
            if numargs > 4:
                return arm_call_long % t
        return arm_call_default % t


    def arm_eabi_genstub(self,fname, flags, idname):
        t = { "fname"  : fname,
              "idname" : idname }
        if flags:
            numargs = int(flags)
            if numargs > 4:
                return arm_eabi_call_long % t
        return arm_eabi_call_default % t


    def thumb_genstub(self,fname, flags, idname):
        t = { "fname"  : fname,
              "idname" : idname }
        if flags:
            numargs = int(flags)
            if numargs > 4:
                return thumb_call_long % t
        return thumb_call_default % t

    def mips_genstub(self,fname, idname):
        t = { "fname"  : fname,
              "idname" : idname }
        return mips_call % t

    def process_file(self,input):
        parser = SysCallsTxtParser()
        parser.parse_file(input)
        self.syscalls = parser.syscalls
        parser = None

        for t in self.syscalls:
            syscall_func   = t["func"]
            syscall_params = t["params"]
            syscall_name   = t["name"]

            if t["common"] >= 0 or t["armid"] >= 0:
                num_regs = count_arm_param_registers(syscall_params)
                if gen_thumb_stubs:
                    t["asm-thumb"] = self.thumb_genstub(syscall_func,num_regs,"__NR_"+syscall_name)
                else:
                    if gen_eabi_stubs:
                        t["asm-arm"]   = self.arm_eabi_genstub(syscall_func,num_regs,"__NR_"+syscall_name)
                    else:
                        t["asm-arm"]   = self.arm_genstub(syscall_func,num_regs,"__NR_"+syscall_name)

            if t["common"] >= 0 or t["x86id"] >= 0:
                num_regs = count_generic_param_registers(syscall_params)
                if t["cid"] >= 0:
                    t["asm-x86"] = self.x86_genstub_cid(syscall_func, num_regs, "__NR_"+syscall_name, t["cid"])
                else:
                    t["asm-x86"] = self.x86_genstub(syscall_func, num_regs, "__NR_"+syscall_name)
            elif t["cid"] >= 0:
                E("cid for dispatch syscalls is only supported for x86 in "
                  "'%s'" % syscall_name)
                return
            if t["common"] >= 0 or t["mipsid"] >= 0:
                t["asm-mips"] = self.mips_genstub(syscall_func,"__NR_"+syscall_name)


    def gen_NR_syscall(self,fp,name,id):
        fp.write( "#define __NR_%-25s    (__NR_SYSCALL_BASE + %d)\n" % (name,id) )

    # now dump the content of linux-syscalls.h
    def gen_linux_syscalls_h(self):
        path = "include/sys/linux-syscalls.h"
        D( "generating "+path )
        fp = create_file( path )
        fp.write( "/* auto-generated by gensyscalls.py, do not touch */\n" )
        fp.write( "#ifndef _BIONIC_LINUX_SYSCALLS_H_\n" )
        fp.write( "#define _BIONIC_LINUX_SYSCALLS_H_\n\n" )
        fp.write( "#if !defined __ASM_ARM_UNISTD_H && !defined __ASM_I386_UNISTD_H && !defined __ASM_MIPS_UNISTD_H\n" )
        fp.write( "#if defined __arm__ && !defined __ARM_EABI__ && !defined __thumb__\n" )
        fp.write( "  #  define __NR_SYSCALL_BASE 0x900000\n" )
        fp.write( "#elif defined(__mips__)\n" )
        fp.write( "  #  define __NR_SYSCALL_BASE 4000\n" )
        fp.write( "#else\n" )
        fp.write( "  #  define __NR_SYSCALL_BASE 0\n" )
        fp.write( "#endif\n\n" )

        # first, all common syscalls
        for sc in sorted(self.syscalls,key=lambda x:x["common"]):
            sc_id  = sc["common"]
            sc_name = sc["name"]
            if sc_id >= 0:
                self.gen_NR_syscall( fp, sc_name, sc_id )

        # now, all arm-specific syscalls
        fp.write( "\n#ifdef __arm__\n" );
        for sc in self.syscalls:
            sc_id  = sc["armid"]
            sc_name = sc["name"]
            if sc_id >= 0:
                self.gen_NR_syscall( fp, sc_name, sc_id )
        fp.write( "#endif\n" );

        gen_syscalls = {}
        # finally, all i386-specific syscalls
        fp.write( "\n#ifdef __i386__\n" );
        for sc in sorted(self.syscalls,key=lambda x:x["x86id"]):
            sc_id  = sc["x86id"]
            sc_name = sc["name"]
            if sc_id >= 0 and sc_name not in gen_syscalls:
                self.gen_NR_syscall( fp, sc_name, sc_id )
                gen_syscalls[sc_name] = True
        fp.write( "#endif\n" );

        # all mips-specific syscalls
        fp.write( "\n#ifdef __mips__\n" );
        for sc in sorted(self.syscalls,key=lambda x:x["mipsid"]):
            sc_id = sc["mipsid"]
            if sc_id >= 0:
                self.gen_NR_syscall( fp, sc["name"], sc_id )
        fp.write( "#endif\n" );

        fp.write( "\n#endif\n" )
        fp.write( "\n#endif /* _BIONIC_LINUX_SYSCALLS_H_ */\n" );
        fp.close()
        self.other_files.append( path )

    # now dump the content of linux-syscalls.h
    def gen_linux_unistd_h(self):
        path = "include/sys/linux-unistd.h"
        D( "generating "+path )
        fp = create_file( path )
        fp.write( "/* auto-generated by gensyscalls.py, do not touch */\n" )
        fp.write( "#ifndef _BIONIC_LINUX_UNISTD_H_\n\n" );
        fp.write( "#ifdef __cplusplus\nextern \"C\" {\n#endif\n\n" )

        for sc in self.syscalls:
            fp.write( sc["decl"]+"\n" )

        fp.write( "#ifdef __cplusplus\n}\n#endif\n" )
        fp.write( "\n#endif /* _BIONIC_LINUX_UNISTD_H_ */\n" );
        fp.close()
        self.other_files.append( path )

    # now dump the contents of syscalls.mk
    def gen_arch_syscalls_mk(self, arch):
        path = "arch-%s/syscalls.mk" % arch
        D( "generating "+path )
        fp = create_file( path )
        fp.write( "# auto-generated by gensyscalls.py, do not touch\n" )
        fp.write( "syscall_src := \n" )
        arch_test = {
            "arm": lambda x: x.has_key("asm-arm") or x.has_key("asm-thumb"),
            "x86": lambda x: x.has_key("asm-x86"),
            "mips": lambda x: x.has_key("asm-mips")
        }

        for sc in self.syscalls:
            if arch_test[arch](sc):
                fp.write("syscall_src += arch-%s/syscalls/%s.S\n" %
                         (arch, sc["func"]))
        fp.close()
        self.other_files.append( path )


    # now generate each syscall stub
    def gen_syscall_stubs(self):
        for sc in self.syscalls:
            if sc.has_key("asm-arm") and 'arm' in all_archs:
                fname = "arch-arm/syscalls/%s.S" % sc["func"]
                D2( ">>> generating "+fname )
                fp = create_file( fname )
                fp.write(sc["asm-arm"])
                fp.close()
                self.new_stubs.append( fname )

            if sc.has_key("asm-thumb") and 'arm' in all_archs:
                fname = "arch-arm/syscalls/%s.S" % sc["func"]
                D2( ">>> generating "+fname )
                fp = create_file( fname )
                fp.write(sc["asm-thumb"])
                fp.close()
                self.new_stubs.append( fname )

            if sc.has_key("asm-x86") and 'x86' in all_archs:
                fname = "arch-x86/syscalls/%s.S" % sc["func"]
                D2( ">>> generating "+fname )
                fp = create_file( fname )
                fp.write(sc["asm-x86"])
                fp.close()
                self.new_stubs.append( fname )

            if sc.has_key("asm-mips") and 'mips' in all_archs:
                fname = "arch-mips/syscalls/%s.S" % sc["func"]
                D2( ">>> generating "+fname )
                fp = create_file( fname )
                fp.write(sc["asm-mips"])
                fp.close()
                self.new_stubs.append( fname )

    def  regenerate(self):
        D( "scanning for existing architecture-specific stub files" )

        bionic_root_len = len(bionic_root)

        for arch in all_archs:
            arch_path = bionic_root + "arch-" + arch
            D( "scanning " + arch_path )
            files = glob.glob( arch_path + "/syscalls/*.S" )
            for f in files:
                self.old_stubs.append( f[bionic_root_len:] )

        D( "found %d stub files" % len(self.old_stubs) )

        if not os.path.exists( bionic_temp ):
            D( "creating %s" % bionic_temp )
            make_dir( bionic_temp )

#        D( "p4 editing source files" )
#        for arch in all_archs:
#            commands.getoutput( "p4 edit " + arch + "/syscalls/*.S " )
#            commands.getoutput( "p4 edit " + arch + "/syscalls.mk" )
#        commands.getoutput( "p4 edit " + bionic_root + "include/sys/linux-syscalls.h" )

        D( "re-generating stubs and support files" )

        self.gen_linux_syscalls_h()
        for arch in all_archs:
            self.gen_arch_syscalls_mk(arch)
        self.gen_linux_unistd_h()
        self.gen_syscall_stubs()

        D( "comparing files" )
        adds    = []
        edits   = []

        for stub in self.new_stubs + self.other_files:
            if not os.path.exists( bionic_root + stub ):
                # new file, git add it
                D( "new file:     " + stub)
                adds.append( bionic_root + stub )
                shutil.copyfile( bionic_temp + stub, bionic_root + stub )

            elif not filecmp.cmp( bionic_temp + stub, bionic_root + stub ):
                D( "changed file: " + stub)
                edits.append( stub )

        deletes = []
        for stub in self.old_stubs:
            if not stub in self.new_stubs:
                D( "deleted file: " + stub)
                deletes.append( bionic_root + stub )


        if adds:
            commands.getoutput("git add " + " ".join(adds))
        if deletes:
            commands.getoutput("git rm " + " ".join(deletes))
        if edits:
            for file in edits:
                shutil.copyfile( bionic_temp + file, bionic_root + file )
            commands.getoutput("git add " +
                               " ".join((bionic_root + file) for file in edits))

        commands.getoutput("git add %s%s" % (bionic_root,"SYSCALLS.TXT"))

        if (not adds) and (not deletes) and (not edits):
            D("no changes detected!")
        else:
            D("ready to go!!")

D_setlevel(1)

state = State()
state.process_file(bionic_root+"SYSCALLS.TXT")
state.regenerate()
