from dataclasses import dataclass
from typing import List, Literal
from pprint import pprint
import json
import os
import re
import numpy as np
import yaml
import xml.etree.ElementTree as ET
import matplotlib.pyplot as plt


script_dir = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(script_dir, "reference-files", "X86.json"), "r", encoding="utf-8") as f:
    data = json.load(f)
data = {
    key: value for key, value in data.items() if key != "!instanceof" and isinstance(value, dict)
}  # remove large first key
llvm_instructions = {key: value for key, value in data.items() if "Instruction" in value["!superclasses"]}
llvm_DAGOperands = {key: value for key, value in data.items() if "DAGOperand" in value["!superclasses"]}

# some reasons for missing matches with uops data:
# IMUL8r cannot be matched as LLVM thinks AL is set by the instruction?
# VPDPBSSDSZr / vpdpbssds uops doesnt know this with zmm?
# VDIVPDZ128rrk uops doesnt have all operand combinations

debug = False


def _debug(msg, level=0):
    for _ in range(level):
        msg = "  " + msg
    if debug:
        print(msg)


# expand one or more reg classes recursively to a list of registes
def expand_regs(regs: list | str):
    global llvm_DAGOperands
    _debug(f"expanding {regs}")

    result_regs = []
    if isinstance(regs, str):
        regs = [regs]
    for reg in regs:
        # weird llvm class
        if reg == "GR16orGR32orGR64":
            result_regs += expand_regs(["GR16"])
        if reg == "GR32orGR64":
            result_regs += expand_regs(["GR32"])
        if not reg in llvm_DAGOperands.keys():
            result_regs.append(reg)
            continue
        llvm_reg_class = llvm_DAGOperands[reg]
        if not "MemberList" in llvm_reg_class.keys():
            result_regs.append(reg)  # this not a register class
            continue
        if "%u" in str(llvm_reg_class["MemberList"]["args"]):
            # pattern for registers
            members = llvm_reg_class["MemberList"]["args"]
            base: str = members[0][0]
            # members has pattern and range of numbers to put in pattern
            result_regs += [base.replace("%u", str(i)) for i in range(members[1][0], members[2][0])]
        else:
            # normal list of registers/registerclasses
            result_regs += expand_regs([arg[0]["def"] for arg in llvm_reg_class["MemberList"]["args"]])
    # _debug(str(list(set(result_regs))[:5]) + "...")
    return list(set(result_regs))


def is_same_asm_name(llvm_asm: str, uops_asm: str):
    _debug(f"{llvm_asm}, {uops_asm}")
    # llvm names have those "AsmString": "{cbtw|cbw}", select second variant
    try:
        if llvm_asm[0] == "{":
            llvm_asm = llvm_asm[max(llvm_asm.find("|"), llvm_asm.find("{")) : llvm_asm.find("}")]
        else:
            indices = (
                llvm_asm.find(" "),
                llvm_asm.find("|"),
                llvm_asm.find("{"),
                llvm_asm.find("}"),
                llvm_asm.find("\t"),
            )

            positiveIndices = [i for i in indices if i != -1]
            if positiveIndices and min(positiveIndices) != -1:
                llvm_asm = llvm_asm[0 : min(positiveIndices)]

        llvm_asm = llvm_asm.upper()
    except RuntimeError as e:
        print("isSameAsmName: Error encountered")
        return False

    # there are things like {load} CMP in uops
    start = uops_asm.find("{")
    end = uops_asm.find("}")
    uops_asm = uops_asm.removeprefix(uops_asm[start : end + 1]).strip()
    _debug(f"after process {llvm_asm}, {uops_asm}")
    if llvm_asm != uops_asm:
        return False
    return True


@dataclass
class Operand:
    index: int
    type: Literal["reg", "imm", "flags"]
    width: int
    read: bool
    write: bool
    suppressed: bool
    regList: list

    # note that the index is not relevant when comparing operands as it cannot be guaranteed
    # to be the same for an instruction parsed from uops and one parsed from LLVM
    def __eq__(self, value):
        if not isinstance(value, Operand):
            return NotImplemented
        return (
            self.type == value.type
            and self.read == value.read
            and self.write == value.write
            and self.width == value.width
            and self.suppressed == value.suppressed
            # reg lists dont have to match exactly, but make sure if one has exactly one register the other has, too
            # as those are instructions as XOR AL, I8
            and not ((len(self.regList) != len(value.regList)) and (len(self.regList) == 1 or len(value.regList) == 1))
        )


@dataclass
class Latency:
    startOpIndex: int
    targetOpIndex: int
    cyclesMin: int
    cyclesMax: int


@dataclass
class Instruction:
    asmName: str
    operands: List[Operand]
    throughput_lower: float
    throughput_upper: float
    latencies: List[Latency]
    uopsName: str
    roundc: bool  # AVX512 roundc


def parse_uops_operand(op: ET.Element) -> Operand:
    index = int(op.attrib["idx"]) if "idx" in op.attrib else None
    type = op.attrib["type"] if "type" in op.attrib else None
    if index is None:
        return None
    if type not in ["reg", "imm", "flags"]:
        return None

    read = bool(int(op.attrib.get("r", "0")))
    write = bool(int(op.attrib.get("w", "0")))
    suppressed = bool(int(op.attrib.get("suppressed", "0")))

    if op.text == "0" or op.text == 1:
        return None  # ignore fixed immediates
    if op.text is not None:
        regList = op.text.split(",")
    elif type == "flags":
        regList = ["EFLAGS"]
    else:
        regList = []

    if len(regList) == 1:
        # for some reason fixed registers dont have a width in uops database :(
        width = get_register_width(regList[0])
    else:
        width = int(op.attrib["width"]) if "width" in op.attrib else None
    return Operand(index, type, width, read, write, suppressed, regList)


def parse_uops_latency(lat: ET.Element) -> Latency:
    try:
        startOp = int(lat.attrib["start_op"])
        targetOp = int(lat.attrib["target_op"])
        cycles = int(lat.attrib["cycles"])
    except KeyError:
        # happens e.g. on latency values regarding memory
        return None
    return Latency(startOp, targetOp, cycles, cycles)


def parse_uops_instruction(entry: ET.Element, arch: str):
    if (
        (u_arch := entry.find(f"architecture[@name='{arch}']")) is None
        or (u_operands := entry.findall("operand")) is None
        or (u_m := u_arch.find("measurement")) is None
        or (u_lat := u_m.findall("latency")) is None
    ):
        return None
    operands = [parse_uops_operand(op) for op in u_operands]
    if None in operands:
        return None  # cannot parse all operands
    latencies = [parse_uops_latency(lat) for lat in u_lat]
    try:
        throughput = float(u_m.attrib["TP_loop"])
        uopsAsm = entry.attrib["asm"]
    except KeyError:
        return None
    uopsName = entry.attrib["string"] if "string" in entry.attrib else ""
    roundc = bool(int(entry.attrib["roundc"])) if "roundc" in entry.attrib else False

    return Instruction(uopsAsm, operands, throughput, throughput, latencies, uopsName, roundc)


def parse_uops_database(arch: str) -> List[Instruction]:
    root = ET.parse(os.path.join(script_dir, "reference-files", "uops.xml"))
    u_instrNodes = root.findall(f".//instruction")
    instructions = []
    for entry in u_instrNodes:
        inst = parse_uops_instruction(entry, arch)
        if inst is not None:
            instructions.append(inst)
    return instructions


# AI
def get_other_constraint_side(constraint: str, op: str) -> str | None:
    parts = [part.strip().strip("$") for part in constraint.split("=")]
    if len(parts) != 2:
        return None  # malformed constraint
    if op == parts[0]:
        return parts[1]
    if op == parts[1]:
        return parts[0]
    return None  # op not found


# return all identifiers in constraints without $ e.g. $dst = $src0 -> ["dst", "src0"]
def get_constraints_items(constraint: str):
    parts = [part.strip().strip("$") for part in constraint.split("=")]
    return parts


def get_immidiate_width(imm: str):
    matches = re.findall(r"\d+", imm)
    return int(matches[-1]) if matches else None


def get_register_width(reg_name: str) -> int | None:
    """Return the bit-width of the given LLVM register name for x86.

    Returns:
        int: Width in bits, or None if unknown.
    """
    # AI generated
    # Normalize name (in case someone passes lowercase)
    reg = reg_name.upper()

    # Specific register widths
    known_widths = {
        # FLAGS
        "EFLAGS": None,  # 32,
        "RFLAGS": 64,
        "MXCSR": 32,
        # IP registers
        "IP": 16,
        "EIP": 32,
        "RIP": 64,
        # Segment registers
        "CS": 16,
        "DS": 16,
        "ES": 16,
        "FS": 16,
        "GS": 16,
        "SS": 16,
        # Base addresses
        "FS_BASE": 64,
        "GS_BASE": 64,
        "SSP": 64,
        # MMX
        **{f"MM{i}": 64 for i in range(8)},
        # "MM0": 64, "MM1": 64, "MM2": 64, "MM3": 64, "MM4": 64, "MM5": 64, "MM6": 64, "MM7": 64,
        # FPU registers
        "ST0": 80,
        "ST1": 80,
        "ST2": 80,
        "ST3": 80,
        "ST4": 80,
        "ST5": 80,
        "ST6": 80,
        "ST7": 80,
        "FP0": 80,
        "FP1": 80,
        "FP2": 80,
        "FP3": 80,
        "FP4": 80,
        "FP5": 80,
        "FP6": 80,
        "FP7": 80,
        "FPCW": 16,
        "FPSW": 16,
        # AVX mask registers
        **{f"K{i}": 64 for i in range(8)},
        # Debug & control registers (assume full machine word)
        # **{f"DR{i}": 64 for i in range(16)},
        **{f"CR{i}": 64 for i in range(16)},
        # # Tile registers (AMX)
        # **{f"TMM{i}": 8192 for i in range(8)},
        # "TMMCFG": 64,
    }

    # If it's directly known
    if reg in known_widths:
        return known_widths[reg]
    k_regs = {f"K{i}": 64 for i in range(8)}
    if reg in k_regs:
        return 64

    # Register suffix patterns
    if reg.endswith("B"):  # 8-bit (low)
        return 8
    if reg.endswith("BH"):  # 8-bit (high byte)
        return 8
    if reg.endswith("L"):  # 8-bit (low byte)
        return 8
    if reg.endswith("H"):  # High byte (usually 8-bit)
        if len(reg) <= 3:  # AH, BH, etc.
            return 8
        if reg.endswith("WH"):  # e.g. R10WH
            return 16
        return 8
    if reg.endswith("W"):  # 16-bit
        return 16
    if reg in {"AX", "BX", "CX", "DX", "SI", "DI", "SP", "BP", "IP"}:
        return 16
    if reg.endswith("D"):  # 32-bit
        return 32
    if reg.startswith("E") and len(reg) == 3:  # EAX, EBX, etc.
        return 32
    if reg.startswith("R") and reg[1:].isdigit():  # R8, R10, etc.
        return 64
    if reg.startswith("R") and len(reg) >= 3 and reg[2] not in "BDWH":  # RAX, RBP, etc.
        return 64
    if reg in {"RAX", "RBX", "RCX", "RDX", "RSI", "RDI", "RSP", "RBP"}:
        return 64

    # SIMD vector registers
    if reg.startswith("XMM"):
        return 128
    if reg.startswith("YMM"):
        return 256
    if reg.startswith("ZMM"):
        return 512

    # print(f"unhandled register: {reg_name}")
    return None  # Unknown


def identify_LLVM_operand(opName):
    if opName == "EFLAGS":
        return ("flags", None)
    if opName in llvm_DAGOperands:
        operand = llvm_DAGOperands[opName]
        if "OperandType" in operand and operand["OperandType"] == "OPERAND_IMMEDIATE":
            return ("imm", get_immidiate_width(opName))
        registers = expand_regs(opName)
    else:
        registers = [opName]

    return ("reg", get_register_width(registers[0]))


def parse_LLVM_instruction(LLVMName) -> Instruction:
    global llvm_DAGOperands
    global llvm_instructions
    # idk why some are missing
    if LLVMName not in llvm_instructions:
        return None

    inst = llvm_instructions[LLVMName]
    inOperandList = inst["InOperandList"]["args"]
    outOperandList = inst["OutOperandList"]["args"]
    constraints: str = inst["Constraints"]
    defs = inst["Defs"]
    uses = inst["Uses"]
    # convert operands
    operandList: List[Operand] = []
    index = 1
    roundc = False

    for op in outOperandList:
        if op[1] == "MXCSR":  # uops handles this as a flag, so we dont need it
            continue
        if op[0]["def"] == "AVX512RC":  # llvm has this as operand, uops as flag
            roundc = True
            continue
        type, width = identify_LLVM_operand(op[0]["def"])
        if type is None:
            return None
        elif type == "imm":
            operand = Operand(index, type, width, False, True, False, [])
        else:
            operand = Operand(index, type, width, False, True, False, expand_regs(op[0]["def"]))
        operandList.append(operand)
        index += 1
    for op in inOperandList:
        if op[1] == "MXCSR":  # uops handles this as a flag, so we dont need it
            continue
        if op[0]["def"] == "AVX512RC":  # llvm has this as operand, uops as flag
            roundc = True
            continue
        # process constraints
        wasConstrained = False
        for constraint in constraints.split(","):
            if op[1] is None:
                print("op[1] None")
                return None
            if op[1] not in get_constraints_items(constraint):
                continue
            wasConstrained = True
            # we have to set "read" to True in corresponding def
            dstOp = get_other_constraint_side(constraint, op[1])
            if dstOp is None:
                continue
            defIndex = next((i + 1 for i, defOp in enumerate(outOperandList) if defOp[1] == dstOp), None)
            if defIndex is None:
                return None
            for operand in operandList:
                if operand.index == defIndex:
                    operand.read = True
                    break
        if wasConstrained:
            continue  # do not have to add operand an additional time
        type, width = identify_LLVM_operand(op[0]["def"])
        if type is None:
            return None
        elif type == "imm":
            operand = Operand(index, type, width, True, False, False, [])
        else:
            operand = Operand(index, type, width, True, False, False, expand_regs(op[0]["def"]))
        operandList.append(operand)
        index += 1

    # process defs and uses
    for d in defs:
        opName = d["def"]
        if opName == "MXCSR":  # uops handles this as a flag, so we dont need it
            continue
        type, width = identify_LLVM_operand(opName)
        if type is None:
            return None
        write = True
        read = True if d in uses else False
        regList = [opName] if type == "reg" else []
        if len(regList) == 0:
            regList = ["EFLAGS"] if type == "flags" else []
        # TODO this is not very good yet, there are other registers that are supressed but in here
        suppressed = opName in ["EFLAGS"]
        operand = Operand(index, type, width, read, write, suppressed, regList)
        operandList.append(operand)
        index += 1
    for d in uses:
        if d in defs:
            continue  # already added
        opName = d["def"]
        if opName == "MXCSR":  # uops handles this as a flag, so we dont need it
            continue
        type, width = identify_LLVM_operand(opName)
        if type is None:
            return None
        write = False
        read = True
        regList = [opName] if type == "reg" else []
        if len(regList) == 0:
            regList = ["EFLAGS"] if type == "flags" else []
        suppressed = opName in ["EFLAGS"]  # TODO this is not very good yet
        operand = Operand(index, type, width, read, write, suppressed, regList)
        operandList.append(operand)
        index += 1
    return Instruction(inst["AsmString"], operandList, None, None, [], "", roundc)


def parse_WINIC_instruction(dbEntry) -> Instruction:
    instruction = parse_LLVM_instruction(dbEntry["llvmName"])
    if instruction is None:
        return None
    instruction.throughput_lower = dbEntry.get("throughputMin", None)
    instruction.throughput_upper = dbEntry.get("throughputMax", None)
    operand_latencies = dbEntry.get("operandLatencies", {})
    for lat in operand_latencies:
        sourceOp: str = lat["sourceOperand"]
        # if "ADC16ri" in dbEntry["llvmName"]:
        #     print(lat)
        #     print(lat["sourceOperand"])
        #     exit(1)
        targetOp = lat["targetOperand"]
        if sourceOp.isnumeric():
            sourceIndex = int(sourceOp) + 1  # uops counts from 1, winic from 0
        else:
            # need to find index generated for that operand by parse_LLVM_instruction
            sourceIndex = next(
                (op.index for op in instruction.operands if len(op.regList) == 1 and op.regList[0] == sourceOp), None
            )
        if targetOp.isnumeric():
            targetIndex = int(targetOp) + 1  # uops counts from 1, winic from 0
        else:
            # need to find index generated for that operand by parse_LLVM_instruction
            targetIndex = next(
                (op.index for op in instruction.operands if len(op.regList) == 1 and op.regList[0] == targetOp), None
            )
        if "latencyMin" in lat and "latencyMax" in lat:
            instruction.latencies.append(Latency(sourceIndex, targetIndex, lat["latencyMin"], lat["latencyMax"]))
        else:
            pprint(lat)  # database malformed
            pprint(instruction, compact=True)
            pprint(dbEntry, compact=True)
            exit(1)
    return instruction

# set debug true, dbg instr. to LLVM Name and set uops name to check why two instrucions were not matched
# debug = True
dbgInstruction = ""
dbgUopsInstructionString = ""
# things that should match
# VFMADD132PDZrb VFMADD132PD_ER (ZMM, ZMM, ZMM)
# ADC16ri ADC (R16, I16)
# VSCALEFSSZrr: VSCALEFSS (XMM, XMM, XMM)


def is_same(uopsInst: Instruction, LLVMInst: Instruction):
    global dbgInstruction
    if dbgInstruction != "" and dbgUopsInstructionString not in uopsInst.uopsName:
        return False
    if not is_same_asm_name(LLVMInst.asmName, uopsInst.asmName):
        if dbgInstruction != "":
            print("name")
            pprint(uopsInst, compact=True)
            pprint(LLVMInst, compact=True)
        return False
    if len(uopsInst.operands) != len(LLVMInst.operands):
        if dbgInstruction != "":
            print("numOps")
            pprint(uopsInst, compact=True)
            pprint(LLVMInst, compact=True)
        return False
    if uopsInst.roundc != LLVMInst.roundc:
        if dbgInstruction != "":
            print("roundc")
            pprint(uopsInst, compact=True)
            pprint(LLVMInst, compact=True)
        return False
    # match operands
    llvmOps = LLVMInst.operands.copy()
    for op in uopsInst.operands:
        for lOp in llvmOps:
            if op == lOp:
                llvmOps.remove(lOp)
                break
    if len(llvmOps) != 0:
        if dbgInstruction != "":
            print("not all operands covered")
            pprint(uopsInst, compact=True)
            pprint(LLVMInst, compact=True)
        return False
    return True


@dataclass
class Counters:
    dbProgressC: int
    dbEmptyValueC: int
    internalErrorC: int
    noMatchC: int
    uniqueMatchSameValueC: int
    multiMatchSameValueC: int
    uniqueMatchDiffValueC: int
    multiMatchDiffValueC: int
    noUopsDataC: int


# compare the results with uops data.
def compare(database, type: Literal["lat", "tp"], arch: str) -> Counters:
    # parse measured instructions
    with open(database, "r") as file:
        raw_content = file.read().replace("\t", "    ")  # Replace tabs with 4 spaces
    db = yaml.safe_load(raw_content)
    uops_instructions = parse_uops_database(arch)

    c = Counters(0, 0, 0, 0, 0, 0, 0, 0, 0)
    outputLines = []
    if type == "tp":
        for db_entry in db:
            c.dbProgressC += 1
            if c.dbProgressC % 1000 == 0:
                print(c.dbProgressC)
            if dbgInstruction != "" and db_entry["llvmName"] != dbgInstruction:
                continue

            m_cycles = db_entry["throughputMin"]
            if m_cycles == None:
                c.dbEmptyValueC += 1
                continue
            m_instr = parse_WINIC_instruction(db_entry)
            if m_instr is None:
                c.internalErrorC += 1
                continue
            llvm_name = db_entry["llvmName"]

            m_cycles = m_instr.throughput_lower
            # find uops instsruction
            u_matches: List[Instruction] = []
            for u_instr in uops_instructions:
                if is_same(u_instr, m_instr):
                    u_matches.append(u_instr)

            if len(u_matches) == 0:
                outputLines.append(f"{llvm_name}: no match, classify: noMatch\n")
                c.noMatchC += 1
            else:
                # one or multiple matches
                data_match = [
                    0.92 * m_instr.throughput_lower <= u_instr.throughput_lower <= 1.09 * m_instr.throughput_upper
                    for u_instr in u_matches
                ]
                _debug([(u_inst.throughput_lower, m_cycles) for u_inst in u_matches])
                _debug(data_match)

                if False in data_match:
                    outputLines.append(
                        f"{llvm_name}: {u_matches[0].uopsName} uops: {u_matches[0].throughput_lower}, WINIC: {m_cycles}, classify: differentVal(s)\n"
                    )
                    if len(data_match) == 1:
                        c.uniqueMatchDiffValueC += 1
                    else:
                        c.multiMatchDiffValueC += 1

                else:
                    outputLines.append(
                        f"{llvm_name}: {u_matches[0].uopsName} uops: {u_matches[0].throughput_lower}, WINIC: {m_cycles}, classify: matchingVal(s)\n"
                    )
                    if len(data_match) == 1:
                        c.uniqueMatchSameValueC += 1
                    else:
                        c.multiMatchSameValueC += 1

        with open(os.path.join(script_dir, "compareTP.log"), "w") as out_file:
            out_file.writelines(outputLines)

    if type == "lat":
        for db_entry in db:
            llvm_name = db_entry["llvmName"]
            m_instr = parse_WINIC_instruction(db_entry)
            if m_instr is None:
                c.internalErrorC += 1
                continue

            c.dbProgressC += len(m_instr.latencies)
            if c.dbProgressC % 1000 == 0:
                print(c.dbProgressC)
            # find uops inststruction
            u_matches: List[Instruction] = []
            for u_instr in uops_instructions:
                if is_same(u_instr, m_instr):
                    u_matches.append(u_instr)

            if len(u_matches) == 0:
                outputLines.append(f"{llvm_name}: no match, classify: noMatch\n")
                for lat in m_instr.latencies:
                    if lat.cyclesMin != None:
                        c.noMatchC += 1
                    else:
                        c.dbEmptyValueC += 1

                continue

            # if u_instr.uopsName != "VDIVPD (XMM, K, XMM, XMM)":
            #     continue
            # one or multiple matches
            for m_lat in m_instr.latencies:
                if m_lat.cyclesMin == None:
                    c.dbEmptyValueC += 1
                    continue
                data_match = []
                for u_instr in u_matches:
                    # find the corresponding latency value in the uops instruction
                    # first get the actual operands
                    try:
                        m_src_op = next(op for op in m_instr.operands if op.index == m_lat.startOpIndex)
                        m_dst_op = next(op for op in m_instr.operands if op.index == m_lat.targetOpIndex)
                    except StopIteration:
                        print("fatal error, latency result references an non-existing operand (unreachable)")
                        pprint(m_instr)
                        exit(1)

                    # get all uops operands that could correspond to the current winic ones
                    u_src_candidates = [op for op in u_instr.operands if op == m_src_op]
                    u_dst_candidates = [op for op in u_instr.operands if op == m_dst_op]
                    # select the correct candidate
                    # if there are multiple operands that fulfill the == constraint TODO currently just fail
                    if len(u_src_candidates) == 0 or len(u_dst_candidates) == 0:
                        # this should never happen, unless the instructions were matched incorrectly
                        print("alarm")
                        exit(1)
                    if len(u_src_candidates) > 1:
                        # if there are multiple operands with same read/write/register combination,
                        # we assume they are in the same order for both uops and winic database
                        # therefore this is written in a way so it doesn't matter which indices the operands have, only that the order is right
                        # all the operands with same properties from winic
                        m_src_candidates = [op for op in m_instr.operands if op == m_src_op]
                        # the index of the current operand in m_src_candidates
                        m_index_in_list = next(i for i, op in enumerate(m_src_candidates) if op.index == m_src_op.index)
                        # take the element at the same index from u_src_candidates
                        u_src_op = u_src_candidates[m_index_in_list]
                    else:
                        u_src_op = u_src_candidates[0]
                    if len(u_dst_candidates) > 1:
                        m_dst_candidates = [op for op in m_instr.operands if op == m_dst_op]
                        m_index_in_list = next(i for i, op in enumerate(m_dst_candidates) if op.index == m_dst_op.index)
                        u_dst_op = u_dst_candidates[m_index_in_list]
                    else:
                        u_dst_op = u_dst_candidates[0]

                    # extract the uops latency result
                    try:
                        u_lat = next(
                            lat
                            for lat in u_instr.latencies
                            if lat.startOpIndex == u_src_op.index and lat.targetOpIndex == u_dst_op.index
                        )
                    except StopIteration:
                        continue
                    if m_lat.cyclesMin <= u_lat.cyclesMin and u_lat.cyclesMin <= m_lat.cyclesMax:
                        data_match.append(True)
                        outputLines.append(
                            f"{llvm_name}: {u_instr.uopsName} {u_lat.startOpIndex} -> {u_lat.targetOpIndex} uops: {u_lat.cyclesMin}, WINIC: {m_lat.cyclesMin}-{m_lat.cyclesMax}, classify: sameVal\n"
                        )
                    else:
                        data_match.append(False)
                        outputLines.append(
                            f"{llvm_name}: {u_instr.uopsName} {u_lat.startOpIndex} -> {u_lat.targetOpIndex} uops: {u_lat.cyclesMin}, WINIC: {m_lat.cyclesMin}-{m_lat.cyclesMax}, classify: differentVal\n"
                        )
                if len(data_match) == 0:
                    c.noUopsDataC += 1
                elif False in data_match:
                    if len(data_match) == 1:
                        c.uniqueMatchDiffValueC += 1
                    else:
                        c.multiMatchDiffValueC += 1

                elif all(data_match):
                    if len(data_match) == 1:
                        c.uniqueMatchSameValueC += 1
                    else:
                        c.multiMatchSameValueC += 1

        with open(os.path.join(script_dir, "compareLAT.log"), "w") as out_file:
            out_file.writelines(outputLines)

    print(f"{c.dbProgressC} total database entries")
    print(f"{c.dbProgressC-c.dbEmptyValueC} entries have values")
    print(f"{c.uniqueMatchSameValueC} values match with exactly one uops instruction")
    print(f"{c.multiMatchSameValueC} values match with multiple uops instructions which all have the same value")
    print(f"{c.multiMatchDiffValueC} values were matched with multiple uops instructions with different values")
    print(f"{c.uniqueMatchDiffValueC} values don't match with uops data")
    print(f"{c.noMatchC} values could not be matched with an instruction from uops")
    print(f"{c.internalErrorC} internal errors occurred")
    print(f"{c.noUopsDataC} values were matched but uops has no data")
    total_matching = c.uniqueMatchSameValueC + c.multiMatchSameValueC
    total_non_matching = c.uniqueMatchDiffValueC + c.multiMatchDiffValueC
    print(
        f"{(total_matching)*100/(total_matching+total_non_matching):.2f}% of values are the same (excluding missing matches)"
    )
    return c


def count_ranges(database) -> int:
    # parse measured instructions
    with open(database, "r") as file:
        raw_content = file.read().replace("\t", "    ")  # Replace tabs with 4 spaces
    db = yaml.safe_load(raw_content)
    tp_range_counter = 0
    tp_exact_counter = 0
    lat_range_counter = 0
    lat_exact_counter = 0
    for db_entry in db:
        m_instr = parse_WINIC_instruction(db_entry)
        if m_instr.throughput_lower != None:
            if m_instr.throughput_lower != m_instr.throughput_upper:
                tp_range_counter += 1
            else:
                tp_exact_counter += 1

        for lat_entry in m_instr.latencies:
            if lat_entry.cyclesMin != None:
                if lat_entry.cyclesMin != lat_entry.cyclesMax:
                    lat_range_counter += 1
                else:
                    lat_exact_counter += 1
    print(f"{tp_range_counter=}")
    print(f"{tp_exact_counter=}")
    print(f"{lat_range_counter=}")
    print(f"{lat_exact_counter=}")
    print(f"proportion TP ranges: {tp_range_counter/(tp_range_counter+tp_exact_counter):.2f}")
    print(f"proportion LAT ranges: {lat_range_counter/(lat_range_counter+lat_exact_counter):.2f}")


def plotTP(values):
    categories = [
        "one match\nsame value",
        "multiple matches\nall same value",
        "multiple matches\ndifferent values",
        "one match\ndifferent value",
        "no match",
    ]

    def no_zero_autopct(pct):
        return f"{pct:.1f}%" if pct > 0 else ""

    if len(values) == 0:
        vals = np.array([[3179.0, 1949.0], [39.0, 314.0], [258, 0]])
    else:
        vals = np.array([[values[0], values[1]], [values[2], values[3]], [values[4], 0]])

    tab20c = plt.color_sequences["tab20c"]
    outer_colors = [tab20c[i] for i in [8, 4, 17]]
    inner_colors = [tab20c[i] for i in [9, 11, 7, 5, 17, 1]]
    fig, ax = plt.subplots()  # figsize=(8, 6)
    ax.set_position([0.25, 0.1, 0.6, 0.8])
    size = 0.3
    acc_wedges, _, _ = ax.pie(
        vals.sum(axis=1),
        radius=1 - size,
        colors=outer_colors,
        wedgeprops=dict(width=size, edgecolor="w"),
        autopct=no_zero_autopct,
        pctdistance=0.77,
    )
    wedges, _, _ = ax.pie(
        vals.flatten(),
        radius=1,
        colors=inner_colors,
        wedgeprops=dict(width=size, edgecolor="w"),
        autopct=no_zero_autopct,
        pctdistance=0.85,
    )
    ax.set(aspect="equal")  # keep the pie circular
    ax.set(title="Comparison between WINIC and uops.info (Throughput)")
    outer_legend = ax.legend(wedges, categories, bbox_to_anchor=(0.9, 0.5))
    inner_legend = ax.legend(acc_wedges, ["total same value", "total different value"], bbox_to_anchor=(0.9, 0.4))
    ax.add_artist(outer_legend)
    ax.add_artist(inner_legend)
    plt.tight_layout()
    plt.savefig(os.path.join(script_dir, "TP_chart.png"))


def plotLAT(values):
    categories = [
        "same value",
        "different values",
        "no match",
    ]

    def no_zero_autopct(pct):
        return f"{pct:.1f}%" if pct > 0 else ""

    if len(values) == 0:
        values = [9331, 1526, 413]

    tab20c = plt.color_sequences["tab20c"]
    outer_colors = [tab20c[i] for i in [8, 4, 17]]
    fig, ax = plt.subplots()
    ax.set_position([0.25, 0.1, 0.6, 0.8])
    wedges, _, _ = ax.pie(
        values,
        colors=outer_colors,
        wedgeprops=dict(edgecolor="w"),
        autopct=no_zero_autopct,
    )
    ax.set(aspect="equal")  # keep the pie circular
    ax.set(title="Comparison between WINIC and uops.info (Latency)")
    ax.legend(wedges, categories, bbox_to_anchor=(0.9, 0.9))  #
    plt.tight_layout()
    plt.savefig(os.path.join(script_dir, "LAT_chart.png"))


def plotLAT2(values):
    categories = [
        "same value",
        "different values",
        "no match",
    ]

    def no_zero_autopct(pct):
        return f"{pct:.1f}%" if pct > 0 else ""

    if len(values) == 0:
        values = [9331, 1526, 413]

    tab20c = plt.color_sequences["tab20c"]
    outer_colors = [tab20c[i] for i in [8, 4, 17]]
    fig, ax = plt.subplots()
    ax.set_position([0.25, 0.1, 0.6, 0.8])
    wedges, _, _ = ax.pie(
        values,
        colors=outer_colors,
        wedgeprops=dict(edgecolor="w"),
        autopct=no_zero_autopct,
    )
    ax.set(aspect="equal")  # keep the pie circular
    ax.set(title="Comparison between WINIC and uops.info (Latency)")
    ax.legend(wedges, categories, bbox_to_anchor=(0.9, 0.9))  #
    plt.tight_layout()
    plt.savefig(os.path.join(script_dir, "LAT_chart.png"))


def plot_combined(lat: Counters, tp: Counters):
    categoriesTP = [
        "one match\nsame value",
        "multiple matches\nall same value",
        "multiple matches\ndifferent values",
        "one match\ndifferent value",
        # "no match",
    ]

    colors = [
        "#91cf60",  # medium green
        "#d9ef8b",  # light green
        "#fee08b",  # yellow
        "#fc8d59",  # orange
        "grey",
        "grey",
    ]

    def no_zero_autopct(pct):
        return f"{pct:.1f}%" if pct > 0 else ""

    tab20c = plt.color_sequences["tab20c"]

    # inner_colors = [tab20c[i] for i in [8, 4, 17]]
    # outer_colors = [tab20c[i] for i in [9, 11, 7, 5, 17, 1]]
    inner_colors = ["#008000", "#ff0000", "grey"]
    outer_colors = [
        "#6fbe59",
        "#bfffa7",
        "#ffd8b3",
        "#ff914d",
        "grey",
        "grey",
    ]
    # outer_colors = colors
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 5))
    ax1.set(aspect="equal")  # keep the pie circular
    ax2.set(aspect="equal")  # keep the pie circular
    ax1.set(title="Latency")
    ax2.set(title="Throughput")
    size = 0.3

    # reference values are Zen4
    if lat is None:
        lat = np.array([[5871, 4275], [240, 494], [1345, 0]])
    else:
        lat = np.array(
            [
                [lat.uniqueMatchSameValueC, lat.multiMatchSameValueC],
                [lat.multiMatchDiffValueC, lat.uniqueMatchDiffValueC],
                [lat.noMatchC + lat.noUopsDataC, 0],
            ]
        )
    if tp is None:
        tp = np.array([[3173, 1950], [102, 256], [626, 0]])
    else:
        tp = np.array(
            [
                [tp.uniqueMatchSameValueC, tp.multiMatchSameValueC],
                [tp.multiMatchDiffValueC, tp.uniqueMatchDiffValueC],
                [tp.noMatchC + tp.noUopsDataC, 0],
            ]
        )
    # plot LAT
    acc_wedges, _, _ = ax1.pie(
        lat.sum(axis=1),
        radius=1 - size,
        colors=inner_colors,
        wedgeprops=dict(width=size, edgecolor="w"),
        autopct=no_zero_autopct,
        pctdistance=0.77,
    )
    wedges, _, _ = ax1.pie(
        lat.flatten(),
        radius=1,
        colors=outer_colors,
        wedgeprops=dict(width=size, edgecolor="w"),
        autopct=no_zero_autopct,
        pctdistance=0.85,
    )

    # plot TP
    acc_wedges, _, _ = ax2.pie(
        tp.sum(axis=1),
        radius=1 - size,
        colors=inner_colors,
        wedgeprops=dict(width=size, edgecolor="w"),
        autopct=no_zero_autopct,
        pctdistance=0.77,
    )
    wedges, _, _ = ax2.pie(
        tp.flatten(),
        radius=1,
        colors=outer_colors,
        wedgeprops=dict(width=size, edgecolor="w"),
        autopct=no_zero_autopct,
        pctdistance=0.85,
    )
    outer_legend = ax1.legend(wedges, categoriesTP, bbox_to_anchor=(0.93, 0.7))
    inner_legend = ax1.legend(acc_wedges, ["same value", "different value    ", "no match"], bbox_to_anchor=(0.93, 0.9))
    ax1.add_artist(outer_legend)
    ax1.add_artist(inner_legend)
    plt.suptitle("Comparison between WINIC and uops.info")
    plt.tight_layout()
    plt.savefig(os.path.join(script_dir, "combined_chart.png"))


def checkUnique():
    root = ET.parse(os.path.join(script_dir, "reference-files", "uops.xml"))
    instrNodes = root.findall(f".//instruction")
    string_set = {}
    for node in instrNodes:
        string = node.attrib["string"]
        if string in string_set.keys():
            print(f"alarm {string}")
        print(string)
        string_set[string] = True


# some available uops arches:
# CNL, CLX, ICL TGL RKL ADL-P ZEN4
def main(database, arch: str):
    print("Processing Latency")
    lat_res = compare(database, "lat", arch)
    print("Processing Throughput")
    tp_res = compare(database, "tp", arch)
    plot_combined(lat_res, tp_res)


def db_diff(database1, database2, tp, lat):
    with open(database1, "r") as file:
        raw_content = file.read().replace("\t", "    ")  # Replace tabs with 4 spaces
    db1 = yaml.safe_load(raw_content)
    with open(database2, "r") as file:
        raw_content = file.read().replace("\t", "    ")  # Replace tabs with 4 spaces
    db2 = yaml.safe_load(raw_content)
    output = ""

    for entry1 in db1:
        instr1 = parse_WINIC_instruction(entry1)
        instr2 = None
        for entry2 in db2:
            if entry2["llvmName"] == entry1["llvmName"]:
                instr2 = parse_WINIC_instruction(entry2)
                break
        if instr2 == None:
            output += entry1["llvmName"] + " missing in new data"
            # entries1.insert(entry1)
        else:
            # compare
            if tp:
                if instr1.throughput_lower != instr2.throughput_lower:
                    output += f"{entry1["llvmName"]} tpLower {instr1.throughput_lower} -> {instr2.throughput_lower}\n"
                if instr1.throughput_upper != instr2.throughput_upper:
                    output += f"{entry1["llvmName"]} tpUpper {instr1.throughput_upper} -> {instr2.throughput_upper}\n"
            lat_map2 = {(l.startOpIndex, l.targetOpIndex): l for l in instr2.latencies}
            if not lat:
                continue
            for lat1 in instr1.latencies:
                key = (lat1.startOpIndex, lat1.targetOpIndex)
                latString = f'{entry1["llvmName"]} ({lat1.startOpIndex} -> {lat1.targetOpIndex})'
                if key not in lat_map2:
                    output += latString + "missing\n"
                else:
                    if lat1.cyclesMin != lat_map2[key].cyclesMin:
                        output += f"{latString} cyclesMin: {lat1.cyclesMin} -> {lat_map2[key].cyclesMin}\n"
                    if lat1.cyclesMax != lat_map2[key].cyclesMax:
                        output += f"{latString} cyclesMax: {lat1.cyclesMax} -> {lat_map2[key].cyclesMax}\n"
    with open("analysis/diff.txt", "w") as f:
        f.write(output)

def count_uops_tp_vals(arch):
    uops_instructions = parse_uops_database(arch)
    print(f"parsed a total of {len(uops_instructions)} uops instructions")

# main("data/zen4/genoa.yaml", "ZEN4")
# main("build-genoa20/genoa.yaml", "ZEN4")
# db_diff("data/zen4/genoa.yaml","build-genoa20/genoa.yaml", False, True)
# db_diff("data/zen4/genoa.yaml", "build-genoa20/genoa.yaml", False, True)
# plot_combined(None, None)
# count_ranges("data/zen4/genoa.yaml")
count_uops_tp_vals("ZEN4")