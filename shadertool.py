#!/usr/bin/env python3

# TODO: Write utility functions to save indentation and stop copying and
# pasting ;-)

import sys, os, re, argparse, json, itertools, glob, shutil, copy, collections

import shaderutil

tool_name = os.path.basename(sys.argv[0])

def unity_header(name, type):
    if type == 'constant':
        unity_header   = re.compile(r'//(?:\s[0-9a-f]+:)?\s+Vector\s(?P<constant>[0-9]+)\s\[' + name + '\](?:\s[1234]$)?')
        unity53_header = re.compile(r'//\s+' + name + '\s+c(?P<constant>[0-9]+)\s+1$')
    if type == 'matrix':
        unity_header   = re.compile(r'//(?:\s[0-9a-f]+:)?\s+Matrix\s(?P<matrix>[0-9]+)\s\[' + name + '\](?:\s[34]$)?')
        unity53_header = re.compile(r'//\s+' + name + '\s+c(?P<matrix>[0-9]+)\s+3$')
    if type == 'texture':
        unity_header   = re.compile(r'//(?:\s[0-9a-f]+:)?\s+SetTexture\s(?P<texture>[0-9]+)\s\[' + name + '\]\s2D\s(?P<sampler>-?[0-9]+)(?:\sMSAA)?')
        unity53_header = re.compile(r'//\s+' + name + '\s+s(?P<texture>[0-9]+)\s+1$')
    return unity_header, unity53_header


# Regular expressions to match various shader headers:
unity_Ceto_Reflections                = unity_header(r'Ceto_Reflections', 'texture')
unity_CameraDepthTexture              = unity_header(r'_CameraDepthTexture', 'texture')
unity_CameraToWorld                   = unity_header(r'(?:unity)?_CameraToWorld', 'matrix') # Unity 5.4 added unity prefix
unity_FrustumCornersWS                = unity_header(r'_FrustumCornersWS', 'matrix')
unity_glstate_matrix_mvp_pattern      = unity_header(r'glstate_matrix_mvp', 'matrix')
unity_MatrixV_pattern                 = unity_header(r'unity_MatrixV', 'matrix')
unity_MatrixVP_pattern                = unity_header(r'unity_MatrixVP', 'matrix')
unity_Object2World                    = unity_header(r'(?:unity)?_Object(?:2|To)World', 'matrix') # Unity 5.4 added unity prefix and changed '2' to 'To'
unity_WorldSpaceCameraPos             = unity_header(r'_WorldSpaceCameraPos', 'constant')
unity_ZBufferParams                   = unity_header(r'_ZBufferParams', 'constant')
unity_SunPosition                     = unity_header(r'_SunPosition', 'constant')

unreal_DNEReflectionTexture_pattern   = re.compile(r'//\s+DNEReflectionTexture\s+s(?P<texture>[0-9]+)\s+1$')
unreal_NvStereoEnabled_pattern        = re.compile(r'//\s+NvStereoEnabled\s+(?P<constant>c[0-9]+)\s+1$')
unreal_ScreenToLight_pattern          = re.compile(r'//\s+ScreenToLight\s+(?P<constant>c[0-9]+)\s+4$')
unreal_ScreenToShadowMatrix_pattern   = re.compile(r'//\s+ScreenToShadowMatrix\s+(?P<constant>c[0-9]+)\s+4$')
unreal_TextureSpaceBlurOrigin_pattern = re.compile(r'//\s+TextureSpaceBlurOrigin\s+(?P<constant>c[0-9]+)\s+1$')
unreal_ViewProjectionMatrix_pattern   = re.compile(r'//\s+ViewProjectionMatrix\s+(?P<constant>c[0-9]+)\s+4$')
unreal_SceneColorTexture_pattern      = re.compile(r'//\s+SceneColorTexture\s+s(?P<texture>[0-9]+)\s+1$')
unreal_ScreenPositionScaleBias_pattern= re.compile(r'//\s+ScreenPositionScaleBias\s+(?P<constant>c[0-9]+)\s+1$')

# WARNING: THESE REQUIRE THE UNITY HEADERS ATTACHED TO THE SHADER (not a
# problem in 5.2 or older because everything requires that, but starting with
# 5.3 the shaders are no longer stripped and we might be running on a shader
# without Unity headers attached)
unity_shader_directional_lighting     = re.compile(r'//(?:\s[0-9a-f]+:)?\s+Shader\s".*PrePassCollectShadows.*"\s{$')
unity_tag_shadow_caster               = re.compile(r'//(?:\s[0-9a-f]+:)?\s+Tags\s{\s.*"LIGHTMODE"="SHADOWCASTER".*"\s}$')
unity_headers_attached                = re.compile(r"// Headers extracted with DarkStarSword's extract_unity.*_shaders.py")

preferred_stereo_const = 220
dx9settings_ini = {}
collected_errors = []

reg_names = {
    'c': 'Referenced Constants',
    'r': 'Temporary',
    's': 'Samplers',
    'v': 'Inputs',
    'o': 'Outputs',
    'oC': 'Output Colour',
    'oPos': 'Output Position (shader model < 3)',
    'oT': 'Output Texcoord (shader model < 3)',
    'oD': 'Output Colour (shader model < 3)',
    'oFog': 'Output Fog (shader model < 3)',
    'oPts': 'Output Point Size Register (shader model < 3)',
    't': 'Input Texcoord (shader model < 3)',
}

class NoFreeRegisters(Exception): pass
class ExceptionDontReport(Exception): pass

verbosity = 0
def debug(*args, **kwargs):
    print(file=sys.stderr, *args, **kwargs)

def write_ini(*args, **kwargs):
    if sys.stdout.isatty():
        print(*args, **kwargs)
    else:
        # FIXME: Detect newline style from file instead of assuming
        print(*args, end='\r\n', **kwargs)
        debug(*args, **kwargs)

def debug_verbose(level, *args, **kwargs):
    if verbosity >= level:
        return debug(*args, **kwargs)

class InstructionSeparator(object): pass # Newline and semicolon
class Ignore(object): pass # Tokens ignored when analysing shader, but preserved for manipulation (whitespace, comments)
class Strip(object): pass # Removed during tokenisation, not preserved
class StripDuringConversion(object): pass # Removed during shader model upgrade
class Number(object): pass

class Token(str):
    def __new__(cls, string):
        match = cls.pattern.match(string)
        if match is None:
            return None
        ret = str.__new__(cls, match.string[:match.end()])
        return ret

# class Minus(Token):
#     pattern = re.compile(r'-')

class Identifier(Token):
    pattern = re.compile(r'[a-zA-Z_\-!][a-zA-Z_0-9\.]*(\[a[0L](\.[abcdxyzw]{1,4})?(\s*\+\s*\d+)?\](\.[abcdxyzw]{1,4})?)?')

class Comma(Token):
    pattern = re.compile(r',')

class Float(Token, Number):
    pattern = re.compile(r'-?[0-9]*\.[0-9]*(e[-+]?[0-9]+)?')

class Int(Token, Number):
    pattern = re.compile(r'-?[0-9]+')

class CPPStyleComment(Token, Ignore):
    pattern = re.compile(r'\/\/.*$', re.MULTILINE)

class SemiColonComment(Token, Ignore):
    pattern = re.compile(r';.*$', re.MULTILINE)

class HashComment(Token, Ignore):
    pattern = re.compile(r'#.*$', re.MULTILINE)

class CStyleComment(Token, Ignore): # XXX: Are these valid in shader asm language?
    pattern = re.compile(r'\/\*.*\*\/', re.MULTILINE)

class WhiteSpace(Token, Ignore):
    pattern = re.compile(r'[ \t]+')

class NewLine(WhiteSpace, InstructionSeparator):
    pattern = re.compile(r'\n')

class NullByte(Token, Strip):
    pattern = re.compile(r'\0')

    def __str__(self):
        return ''

class Bracket(Token):
    pattern = re.compile(r'(\(|\))') # Seen in a Miasmata preshader

class ParallelPlus(Token, Ignore, StripDuringConversion):
    pattern = re.compile(r'\+')

class Anything(Token):
    pattern = re.compile(r'.')

tokens = (
    CPPStyleComment,
    SemiColonComment, # Seen in declaration section of shaders extracted from Unity like '; 52 ALU, 5 TEX'
    HashComment, # Seen in Stacking with installation path & line numbers
    CStyleComment, # XXX: Are these valid in shader asm?
    NewLine,
    WhiteSpace,
    Comma,
    Float,
    Int,
    # Minus,
    Identifier,
    Bracket,
    NullByte,
    ParallelPlus,
    # Anything,
)

def parse_token(shader):
    for t in tokens:
        token = t(shader)
        if token is not None:
            return (token, shader[len(token):])
    try:
        msg = shader.split('\n')[0]
    except:
        msg = shader
    raise SyntaxError(msg)

def tokenise(shader):
    result = []
    while shader:
        (token, shader) = parse_token(shader)
        if not isinstance(token, Strip):
            result.append(token)
    return result

class SyntaxTree(list):
    def __repr__(self):
        return '[%s]' % '|'.join([ {True: repr(t), False: str(t)}[isinstance(t, SyntaxTree)] for t in self ])

    def __str__(self):
        return ''.join(map(str, self))

    def iter_all(self):
        '''
        Walk tree returning both tree and leaf nodes. Parent and index is also
        returned for each node to allow for easy replacing.
        '''
        for (i, token) in enumerate(self):
            yield (token, self, i, i)
            if isinstance(token, SyntaxTree):
                for (j, (t, p, pi, _)) in enumerate(token.iter_all()):
                    yield (t, p, pi, i)

    def iter_flat(self):
        '''
        Walk tree returning only leaf nodes
        '''
        for token in self:
            if isinstance(token, SyntaxTree):
                for t in token:
                    yield t
            else:
                yield token

    @staticmethod
    def split_newlines(old_tree): # Also splits on semicolons (untested)
        tree = SyntaxTree([])
        t = SyntaxTree([])
        for token in old_tree:
            if isinstance(token, InstructionSeparator):
                if t:
                    tree.append(t)
                    t = SyntaxTree([])
                tree.append(token)
            else:
                t.append(token)
        if t:
            tree.append(t)
        return tree


class Preshader(SyntaxTree):
    def __init__(self, lst):
        newlst = []
        for line in lst:
            if isinstance(line, Ignore):
                newlst.append(line)
                continue
            for token in line:
                if not isinstance(token, Ignore):
                    newlst.append(CPPStyleComment('//PRESHADER %s' % str(line)))
                    break
            else:
                newlst.append(line)
        SyntaxTree.__init__(self, newlst)

class OpCode(str):
    pass

class Register(str):
    pattern = re.compile(r'''
        (?P<negate>-)?
        (?P<not>!)?
        (?P<type>[a-zA-Z]+)
        (?P<num>\d*)
        (?P<absolute>_abs)?
        (?P<address_reg>
            \[a[0L]
                (?:\.[abcdxyzw]{1,4})?
                (?:\s*\+\s*\d+)?
            \]
        )?
        (?:
            \.
            (?P<swizzle>[abcdxyzw]{1,4})
        )?
        $
    ''', re.VERBOSE)
    def __new__(cls, s):
        match = cls.pattern.match(s)
        if match is None:
            raise SyntaxError('Expected register, found %s' % s)
        ret = str.__new__(cls, s)
        ret.negate = match.group('negate') or ''
        ret.bool_not = match.group('not') or ''
        ret.absolute = match.group('absolute') or ''
        ret.type = match.group('type')
        ret.num = match.group('num')
        ret.reg = ret.type + ret.num # FIXME: Turn into property
        ret.address_reg = match.group('address_reg')
        if ret.num:
            ret.num = int(ret.num)
        ret.swizzle = match.group('swizzle')
        return ret

    def __lt__(self, other):
        if isinstance(self.num, int) and isinstance(other.num, int):
            return self.num < other.num
        return str.__lt__(self, other)

    def __str__(self, negate=None):
        if negate is None:
            negate = self.negate
        r = '%s%s%s%s' % (negate, self.bool_not, self.reg, self.absolute) # FIXME: Sync type and num if reg changed
        if self.address_reg:
            r += self.address_reg
        if self.swizzle:
            r += '.%s' % self.swizzle
        return r

    def __neg__(self):
        if self.negate:
            negate = ''
        else:
            negate = '-'
        return Register(self.__str__(negate))

    # TODO: use __get_attr__ to handle all permutations of this:
    @property
    def x(self): return Register('%s.x' % (self.reg))
    @property
    def y(self): return Register('%s.y' % (self.reg))
    @property
    def z(self): return Register('%s.z' % (self.reg))
    @property
    def w(self): return Register('%s.w' % (self.reg))
    @property
    def xxxx(self): return Register('%s.xxxx' % (self.reg))
    @property
    def yyyy(self): return Register('%s.yyyy' % (self.reg))
    @property
    def xyz(self): return Register('%s.xyz' % (self.reg))

class Instruction(SyntaxTree):
    def is_declaration(self):
        return self.opcode.startswith('dcl_') or self.opcode == 'dcl'

    def is_definition(self):
        return self.opcode == 'def' or self.opcode == 'defi'

    def is_def_or_dcl(self):
        return self.is_definition() or self.is_declaration() or self.opcode in sections

    def refresh_args(self):
        '''
        Call after updating the syntax tree to refresh the instruction arguments
        '''
        self.args = []
        for token in self:
            if isinstance(token, (Register, Number)):
                self.args.append(token)

class NewInstruction(Instruction):
    def __init__(self, opcode, args):
        self.opcode = OpCode(opcode)
        self.args = args
        tree = [self.opcode]
        if args:
            tree.append(WhiteSpace(' '))
            for arg in args[:-1]:
                tree.append(arg)
                tree.append(Comma(','))
                tree.append(WhiteSpace(' '))
            tree.append(args[-1])
        Instruction.__init__(self, tree)

def parse_instruction(line):
    tree = Instruction([])
    tree.opcode = OpCode(line.pop(0))
    tree.args = []
    tree.append(tree.opcode)
    expect = Register
    while (line):
        if all([ isinstance(t, Ignore) for t in line ]):
            break
        token = line.pop(0)
        if isinstance(token, Ignore):
            pass
        # elif isinstance(token, Minus):
        #     if expect != Register and expect != Number:
        #         raise SyntaxError("Expected %s, found %s" % (expect.__name__, token))
        elif isinstance(token, Identifier):
            if expect != Register:
                raise SyntaxError("Expected %s, found %s" % (expect.__name__, token))
            token = Register(token)
            tree.args.append(token)
            expect = Comma
            if tree.is_declaration():
                expect = type(None)
        elif isinstance(token, Number):
            if expect != Number:
                raise SyntaxError("Expected %s, found %s" % (expect.__name__, token))
            tree.args.append(token)
            expect = Comma
        elif isinstance(token, Comma):
            if expect != Comma:
                raise SyntaxError("Expected %s, found %s" % (expect.__name__, token))
            expect = Register
            if tree.is_definition():
                expect = Number
        else:
            raise SyntaxError("Unexpected token: %s" % token)
        tree.append(token)
    return tree

class RegSet(set):
    '''
    Discards swizzle, negate and absolute value modifiers to considder register uniqueness
    '''
    def add(self, reg):
        set.add(self, Register(reg.reg))

class ShaderBlock(SyntaxTree):
    def __init__(self, tree, shader_start):
        newtree = []
        if shader_start is not None:
            self.shader_start = shader_start
            self.decl_end = next_line_pos(self, shader_start)
        in_dcl = True
        for (lineno, line) in enumerate(tree):
            if isinstance(line, Ignore):
                newtree.append(line)
                continue
            t = SyntaxTree([])
            while (line):
                if isinstance(line[0], Ignore):
                    t.append(line.pop(0))
                    continue
                inst = parse_instruction(line)
                if inst.is_def_or_dcl():
                    if not in_dcl:
                        raise SyntaxError("Bad shader: Mixed declarations with code: %s" % inst)
                elif in_dcl:
                    self.decl_end = lineno
                    in_dcl = False
                t.append(inst)
            newtree.append(t)
        SyntaxTree.__init__(self, newtree)

    def insert_decl(self, *inst, comment=None):
        off = 1
        line = SyntaxTree()
        if inst:
            line.append(NewInstruction(*inst))
        if inst and comment:
            line.append(WhiteSpace(' '))
        if comment:
            line.append(CPPStyleComment('// %s' % comment))
        if line:
            self.insert(self.decl_end, line)
            self.decl_end += 1
            off += 1
        self.insert(self.decl_end, NewLine('\n'))
        self.decl_end += 1
        return off

    def insert_instr(self, pos, inst=None, comment=None):
        self.insert(pos, NewLine('\n'))
        line = SyntaxTree()
        if inst:
            line.append(inst)
        if inst and comment:
            line.append(WhiteSpace(' '))
        if comment:
            line.append(CPPStyleComment('// %s' % comment))
        if line:
            self.insert(pos, line)
            return 2
        return 1

    def add_inst(self, *inst):
        if inst:
            self.append(SyntaxTree([NewInstruction(*inst)]))
        self.append(NewLine('\n'))

    def analyse_regs(self, verbose=False):
        def pr_verbose(*args, **kwargs):
            if verbose:
                debug(*args, **kwargs)
        self.local_consts = RegSet()
        self.addressed_regs = RegSet()
        self.declared = []
        self.reg_types = {}
        for (inst, parent, idx, gi) in self.iter_all():
            if not isinstance(inst, Instruction):
                continue
            if inst.is_definition():
                self.local_consts.add(inst.args[0])
                continue
            if inst.is_declaration():
                self.declared.append((inst.opcode, inst.args[0]))
                continue
            for arg in inst.args:
                if arg.type not in self.reg_types:
                    self.reg_types[arg.type] = RegSet()
                self.reg_types[arg.type].add(arg)
                if arg.address_reg:
                    self.addressed_regs.add(arg)

        self.global_consts = self.unref_consts = self.consts = RegSet()
        if 'c' in self.reg_types:
            self.consts = self.reg_types['c']
            self.global_consts = self.consts.difference(self.local_consts)
            self.unref_consts = self.local_consts.difference(self.consts)

        pr_verbose('Local constants: %s' % ', '.join(sorted(self.local_consts)))
        pr_verbose('Global constants: %s' % ', '.join(sorted(self.global_consts)))
        pr_verbose('Unused local constants: %s' % ', '.join(sorted(self.unref_consts)))
        pr_verbose('Declared: %s' % ', '.join(['%s %s' % (k, v) \
                for (k, v) in sorted(self.declared)]))
        for (k, v) in self.reg_types.items():
            pr_verbose('%s: %s' % (reg_names.get(k, k), ', '.join(sorted(v))))

    def _find_free_reg(self, reg_type, model, desired=0):
        if reg_type not in self.reg_types:
            r = Register(reg_type + str(desired))
            self.reg_types[reg_type] = RegSet([r])
            return r

        if model is None:
            model = type(self)

        # Treat all defined constants as taken, even if they aren't used (if we
        # were ever really tight on space we could discard unused local
        # constants):
        taken = self.reg_types[reg_type].union(self.local_consts)
        for num in [desired] + list(range(model.max_regs[reg_type])):
            reg = reg_type + str(num)
            if reg not in taken:
                r = Register(reg)
                self.reg_types[reg_type].add(r)
                return r

        raise NoFreeRegisters(reg_type)

    def do_replacements(self, regs, replace_dcl, insts=None, callbacks=None, re_callbacks=()):
        for (node, parent, idx, gi) in self.iter_all():
            if isinstance(node, Register):
                if not replace_dcl and parent.is_declaration():
                    continue
                if regs is not None and node.reg in regs:
                    # Bit of a hack - Register is supposed to be immutable
                    # since it is a subclass of str, but here we are changing
                    # just part of it, which will get it's string
                    # representation out of sync with itself (and e.g. causes
                    # find_declaration('texcoord', 'v') to fail in a
                    # non-obvious way since the representation starts with 'v',
                    # but the immutable string still starts with 't'). To fix
                    # this, we create a new Register based on the string
                    # representation of the modified register, and we then need
                    # to refresh the copy of the arguments in the parent
                    # instruction since creating a new register means they no
                    # longer point to the same one.
                    node.reg = regs[node.reg]
                    parent[idx] = Register(str(node))
                    parent.refresh_args()
            if isinstance(node, Instruction):
                if insts is not None and node.opcode in insts:
                    parent[idx] = insts[node.opcode]
                if callbacks is not None and node.opcode in callbacks:
                    callbacks[node.opcode](self, node, parent, idx)
                for (exp, cb) in re_callbacks:
                    match = exp.match(node.opcode)
                    if match:
                        cb(self, node, parent, idx, match, gi)
            if isinstance(node, StripDuringConversion):
                parent[idx] = WhiteSpace(' ')

    def discard_if_unused(self, regs, reason = 'unused'):
        self.analyse_regs()
        discard = self.unref_consts.intersection(RegSet(regs))
        if not discard:
            return
        for (node, parent, idx, gi) in self.iter_all():
            if isinstance(node, Instruction) and node.is_definition() and node.args[0] in discard:
                parent[idx] = CPPStyleComment('// Discarded %s constant %s' % (reason, node.args[0]))

def fixup_sincos(tree, node, parent, idx):
    parent[idx] = NewInstruction('sincos', (node.args[0], node.args[1]))
    tree.discard_if_unused((node.args[2], node.args[3]), 'sincos')

def fixup_mova(tree, node, parent, idx):
    if node.args[0].reg == 'a0':
        parent[idx] = NewInstruction('mova', (node.args[0], node.args[1]))

class VertexShader(ShaderBlock): pass
class PixelShader(ShaderBlock): pass

class VS3(VertexShader):
    model = 'vs_3_0'

    max_regs = { # http://msdn.microsoft.com/en-us/library/windows/desktop/bb172963(v=vs.85).aspx
        'c': 256,
        'i': 16,
        'o': 12,
        'r': 32,
        's': 4,
        'v': 16,
    }
    def_stereo_sampler = 's0'

class PS3(PixelShader):
    max_regs = { # http://msdn.microsoft.com/en-us/library/windows/desktop/bb172920(v=vs.85).aspx
        'c': 224,
        'i': 16,
        'r': 32,
        's': 16,
        'v': 12,
    }
    def_stereo_sampler = 's13'

def vs_to_shader_model_3_common(shader, shader_model, args, extra_fixups = {}):
    shader.analyse_regs()
    shader.insert_decl()
    replace_regs = {}

    if 'oT' in shader.reg_types:
        for reg in sorted(shader.reg_types['oT']):
            opcode = 'dcl_texcoord'
            if reg.num:
                opcode = 'dcl_texcoord%d' % reg.num
            out = shader._find_free_reg('o', VS3)
            shader.insert_decl(opcode, [out])
            replace_regs[reg.reg] = out

    if 'oPos' in shader.reg_types:
        out = shader._find_free_reg('o', VS3)
        shader.insert_decl('dcl_position', [out])
        replace_regs['oPos'] = out


    if 'oD' in shader.reg_types:
        for reg in sorted(shader.reg_types['oD']):
            opcode = 'dcl_color'
            if reg.num:
                opcode = 'dcl_color%d' % reg.num
            out = shader._find_free_reg('o', VS3)
            shader.insert_decl(opcode, [out])
            replace_regs[reg.reg] = out

    if 'oFog' in shader.reg_types:
        out = shader._find_free_reg('o', VS3)
        shader.insert_decl('dcl_fog', [out])
        replace_regs['oFog'] = out
    if 'oPts' in shader.reg_types:
        out = shader._find_free_reg('o', VS3)
        shader.insert_decl('dcl_psize', [out])
        replace_regs['oPts'] = out

    shader.insert_decl()

    fixups = {'sincos': fixup_sincos}
    fixups.update(extra_fixups)

    shader.do_replacements(replace_regs, True, {shader_model: 'vs_3_0'}, fixups)

    insert_converted_by(shader, shader_model) # Do this before changing the class!
    shader.__class__ = VS3

    if (args.add_fog_on_sm3_update):
        shader.analyse_regs()
        add_unity_autofog_VS3(shader, reason="for fog compatibility on upgrade from %s to vs_3_0" % shader_model)

def insert_converted_by(tree, orig_model):
    pos = prev_line_pos(tree, tree.shader_start)
    tree[tree.shader_start].append(WhiteSpace(' '))
    tree[tree.shader_start].append(CPPStyleComment("// Converted from %s with DarkStarSword's shadertool.py" % orig_model))

class VS11(VertexShader):
    model = 'vs_1_1'

    def to_shader_model_3(self, args):
        # NOTE: Only very lightly tested!
        vs_to_shader_model_3_common(self, self.model, args, {'mov': fixup_mova})

class VS2(VertexShader):
    model = 'vs_2_0'

    def to_shader_model_3(self, args):
        vs_to_shader_model_3_common(self, self.model, args)

class PS11(PixelShader):
    model = 'ps_1_1'

    x2 = re.compile(r'^(?P<before>.+)(?P<modifier>_x2)(?P<after>.*)$')
    x4 = re.compile(r'^(?P<before>.+)(?P<modifier>_x4)(?P<after>.*)$')
    x8 = re.compile(r'^(?P<before>.+)(?P<modifier>_x8)(?P<after>.*)$')
    d2 = re.compile(r'^(?P<before>.+)(?P<modifier>_d2)(?P<after>.*)$')
    d4 = re.compile(r'^(?P<before>.+)(?P<modifier>_d4)(?P<after>.*)$')
    d8 = re.compile(r'^(?P<before>.+)(?P<modifier>_d8)(?P<after>.*)$')

    def to_shader_model_3(self, args):
        self.analyse_regs()
        self.insert_decl()
        replace_regs = {}

        # For _x* and _d* instruction modifier fixups:
        mul_reg = self._find_free_reg('c', PS3)
        div_reg = self._find_free_reg('c', PS3)
        self.insert_decl('def', [mul_reg, '2', '4', '8', '0'])
        self.insert_decl('def', [div_reg, '0.5', '0.25', '0.125', '0'])

        if 'v' in self.reg_types:
            for reg in sorted(self.reg_types['v']):
                opcode = 'dcl_color'
                if reg.num:
                    opcode = 'dcl_color%d' % reg.num
                self.insert_decl(opcode, [reg])

        if 't' in self.reg_types:
            for reg in sorted(self.reg_types['t']):
                out = self._find_free_reg('r', PS3)
                replace_regs[reg.reg] = out

        new_dcls = []

        def fixup_ps11_tex(tree, node, parent, idx):
            reg = node.args[0]
            dcl = 'dcl_texcoord%d' % reg.num
            vreg = self._find_free_reg('v', PS3)
            sreg = 's%d' % reg.num
            new_dcls.append(('dcl_texcoord%d' % reg.num, [vreg]))
            new_dcls.append(('dcl_2d', [sreg])) # FIXME: What if it's not a Tex2D?
            parent[idx] = NewInstruction('texld', (reg, vreg, sreg))

        def fixup_ps11_modifier(tree, node, parent, idx, match, gi):
            reg = node.args[0]
            node.opcode = OpCode(match.group('before') + match.group('after'))
            node[0] = node.opcode
            modifier = match.group('modifier')
            if modifier[1] == 'x':
                creg = mul_reg
            elif modifier[1] == 'd':
                creg = div_reg
            if modifier[2] == '2':
                component = 'x'
            elif modifier[2] == '4':
                component = 'y'
            elif modifier[2] == '8':
                component = 'z'
            tree.insert_instr(gi + 2, NewInstruction('mul', [reg, reg, '%s.%s' % (creg, component)]))

        self.do_replacements(replace_regs, True, {self.model: 'ps_3_0'},
                {'tex': fixup_ps11_tex}, (
                    (self.x2, fixup_ps11_modifier),
                    (self.x4, fixup_ps11_modifier),
                    (self.x8, fixup_ps11_modifier),
                    (self.d2, fixup_ps11_modifier),
                    (self.d4, fixup_ps11_modifier),
                    (self.d8, fixup_ps11_modifier),
                ))

        for dcl in new_dcls:
            self.insert_decl(*dcl)
        self.insert_decl()

        self.add_inst('mov', ['oC0', 'r0'])

        insert_converted_by(self, self.model) # Do this before changing the class!
        self.__class__ = PS3


class PS2(PixelShader):
    model = 'ps_2_0'

    def to_shader_model_3(self, args):
        def fixup_ps2_dcl(tree, node, parent, idx):
            modifier = node.opcode[3:]
            reg = node.args[0]
            if reg.type == 'v':
                node.opcode = OpCode('dcl_color%s' % modifier)
                if reg.num:
                    node.opcode = OpCode('dcl_color%d%s' % (reg.num, modifier))
            elif reg.type == 't':
                node.opcode = OpCode('dcl_texcoord%s' % modifier)
                if reg.num:
                    node.opcode = OpCode('dcl_texcoord%d%s' % (reg.num, modifier))
            node[0] = node.opcode
        self.analyse_regs()
        replace_regs = {}

        if 't' in self.reg_types:
            for reg in sorted(self.reg_types['t']):
                replace_regs[reg.reg] = self._find_free_reg('v', PS3)

        self.do_replacements(replace_regs, True, {self.model: 'ps_3_0'},
                {'sincos': fixup_sincos, 'dcl': fixup_ps2_dcl,
                'dcl_centroid': fixup_ps2_dcl, 'dcl_pp': fixup_ps2_dcl})
        insert_converted_by(self, self.model) # Do this before changing the class!
        self.__class__ = PS3

class VS2X(VS2):
    # Need to verify, but this looks like the same conversion as vs_2_0 should
    # work
    model = 'vs_2_x'

class PS2X(PS2):
    # Need to verify, but this looks like the same conversion as ps_2_0 should
    # work
    model = 'ps_2_x'

sections = {
    'vs_3_0': VS3,
    'ps_3_0': PS3,
    'vs_2_0': VS2,
    'vs_2_x': VS2X,
    'ps_2_0': PS2,
    'ps_2_x': PS2X,
    'vs_1_1': VS11,
    # 'ps_1_1': PS11, # Not enabling yet as it is pretty alpha code
}

def process_sections(tree):
    '''
    Preshader will be commented out, main shader will by analysed and turned
    into a series of instructions
    '''
    preshader_start = None
    for (lineno, line) in enumerate(tree):
        if not isinstance(line, SyntaxTree):
            continue
        for token in line:
            if isinstance(token, Ignore):
                continue
            if token == 'preshader':
                if preshader_start is not None:
                    raise SyntaxError('Multiple preshader blocks')
                preshader_start = lineno
                # debug('Preshader found starting on line %i' % lineno)
            elif token in sections:
                # debug('Identified shader type %s' % token)
                head = SyntaxTree(tree[:lineno])
                preshader = SyntaxTree([])
                if preshader_start is not None:
                    head = SyntaxTree(tree[:preshader_start])
                    preshader = Preshader(tree[preshader_start:lineno])
                return sections[token](head + preshader + tree[lineno:], lineno)
            elif preshader_start is None:
                raise SyntaxError('Unexpected token while searching for shader type: %s' % token)
    raise SyntaxError('Unable to identify shader type')

def parse_shader(shader, args = None):
    tokens = tokenise(shader)
    if args and args.debug_tokeniser:
        for token in tokens:
            debug('%s: %s' % (token.__class__.__name__, repr(str(token))))
    tree = SyntaxTree(tokens)
    tree = SyntaxTree.split_newlines(tree)
    tree = process_sections(tree)
    return tree

def install_shader_to(shader, file, args, base_dir, show_full_path=False):
    try:
        os.mkdir(base_dir)
    except OSError:
        pass

    override_dir = os.path.join(base_dir, 'ShaderOverride')
    try:
        os.mkdir(override_dir)
    except OSError:
        pass

    if isinstance(shader, VertexShader):
        shader_dir = os.path.join(override_dir, 'VertexShaders')
    elif isinstance(shader, PixelShader):
        shader_dir = os.path.join(override_dir, 'PixelShaders')
    else:
        raise Exception("Unrecognised shader type: %s" % shader.__class__.__name__)
    try:
        os.mkdir(shader_dir)
    except OSError:
        pass

    dest_name = '%s.txt' % shaderutil.get_filename_crc(file)
    dest = os.path.join(shader_dir, dest_name)
    if not args.force and os.path.exists(dest):
        debug_verbose(0, 'Skipping %s - already installed' % file)
        return False

    if show_full_path:
        debug_verbose(0, 'Installing to %s...' % dest)
    else:
        debug_verbose(0, 'Installing to %s...' % os.path.relpath(dest, os.curdir))
    print(shader, end='', file=open(dest, 'w'))

    return True # Returning success will allow ini updates

def install_shader(shader, file, args):
    if not (isinstance(shader, (VS3, PS3))):
        raise Exception("Shader must be a vs_3_0 or a ps_3_0, but it's a %s" % shader.__class__.__name__)

    src_dir = os.path.dirname(os.path.join(os.curdir, file))
    dumps = os.path.realpath(os.path.join(src_dir, '../..'))
    if os.path.basename(dumps).lower() != 'dumps':
        raise Exception("Not installing %s - not in a Dumps directory" % file)
    gamedir = os.path.realpath(os.path.join(src_dir, '../../..'))

    return install_shader_to(shader, file, args, gamedir)

def _check_shader_installed(shader_dir, file):
    dest_name = '%s.txt' % shaderutil.get_filename_crc(file)
    dest = os.path.join(shader_dir, dest_name)
    return os.path.exists(dest)

def check_shader_installed(file, args):
    # TODO: Refactor common code with install functions
    src_dir = os.path.realpath(os.path.dirname(os.path.join(os.curdir, file)))

    if args.install_to:
        # If using -I we aren't in the dumps directory so we won't know if it's
        # a vertex or pixel shader until we parse it. We still want the
        # pre-check though, so just check both possible destinations:
        vertex_path = os.path.join(args.install_to, 'ShaderOverride/VertexShaders')
        pixel_path = os.path.join(args.install_to, 'ShaderOverride/PixelShaders')
        return _check_shader_installed(vertex_path, file) or _check_shader_installed(pixel_path, file)

    dumps = os.path.realpath(os.path.join(src_dir, '../..'))
    if os.path.basename(dumps).lower() != 'dumps':
        raise Exception("Not checking if %s installed - not in a Dumps directory" % file)
    gamedir = os.path.realpath(os.path.join(src_dir, '../../..'))

    override_dir = os.path.join(gamedir, 'ShaderOverride')

    if os.path.basename(src_dir).lower().startswith('vertex'):
        shader_dir = os.path.join(override_dir, 'VertexShaders')
    elif os.path.basename(src_dir).lower().startswith('pixel'):
        shader_dir = os.path.join(override_dir, 'PixelShaders')
    else:
        raise ValueError("Couldn't determine type of shader from directory")

    return _check_shader_installed(shader_dir, file)

def find_game_dir(file):
    src_dir = os.path.dirname(os.path.join(os.curdir, file))
    parent = os.path.realpath(os.path.join(src_dir, '..'))
    if os.path.basename(parent).lower() == 'shaderoverride':
        return os.path.realpath(os.path.join(parent, '..'))
    parent = os.path.realpath(os.path.join(parent, '..'))
    if os.path.basename(parent).lower() != 'dumps':
        raise ValueError('Unable to find game directory')
    return os.path.realpath(os.path.join(parent, '..'))

def get_alias(game):
    try:
        with open(os.path.join(os.path.dirname(__file__), '.aliases.json'), 'r', encoding='utf-8') as f:
            aliases = json.load(f)
            return aliases.get(game, game)
    except IOError:
        return game

def game_git_dir(game_dir):
    game = os.path.basename(game_dir)
    script_dir = os.path.dirname(__file__)
    alias = get_alias(game)
    return os.path.join(script_dir, alias)

def install_shader_to_git(shader, file, args):
    game_dir = find_game_dir(file)

    # Filter out common subdirectory names:
    blacklisted_names = ('win32', 'win64', 'binaries', 'bin', 'win_x86', 'win_x64')
    while os.path.basename(game_dir).lower() in blacklisted_names:
        game_dir = os.path.realpath(os.path.join(game_dir, '..'))

    dest_dir = game_git_dir(game_dir)

    return install_shader_to(shader, file, args, dest_dir, True)

def find_original_shader(file):
    game_dir = find_game_dir(file)
    crc = shaderutil.get_filename_crc(file)
    src_dir = os.path.realpath(os.path.dirname(os.path.join(os.curdir, file)))
    if os.path.basename(src_dir).lower().startswith('vertex'):
        pattern = 'Dumps/AllShaders/VertexShader/%s.txt' % crc
    elif os.path.basename(src_dir).lower().startswith('pixel'):
        pattern = 'Dumps/AllShaders/PixelShader/CRC32_%s_*.txt' % (crc.lstrip('0'))
    else:
        raise ValueError("Couldn't determine type of shader from directory")
    pattern = os.path.join(game_dir, pattern)
    files = glob.glob(pattern)
    if not files:
        raise OSError('Unable to find original shader for %s: %s not found' % (file, pattern))
    return files[0]

def restore_original_shader(file):
    try:
        shutil.copyfile(find_original_shader(file), file)
    except OSError as e:
        debug(str(e))

class StereoSamplerAlreadyInUse(Exception): pass

def insert_stereo_declarations(tree, args, x=0, y=1, z=0.0625, w=0.5):
    if hasattr(tree, 'stereo_const'):
        return tree.stereo_const, 0

    if isinstance(tree, VertexShader) and args.use_nv_stereo_reg_vs:
        # When using the undocumented stereo register we don't insert a texture
        # declaration, but we still insert c220 because a bunch of code will
        # try to use it
        tree.stereo_sampler = None
        tree.nv_stereo_reg = Register(args.use_nv_stereo_reg_vs)
    elif isinstance(tree, VertexShader) and args.stereo_sampler_vs:
        tree.stereo_sampler = args.stereo_sampler_vs
        if 's' in tree.reg_types and tree.stereo_sampler in tree.reg_types['s']:
            raise StereoSamplerAlreadyInUse(tree.stereo_sampler)
    elif isinstance(tree, PixelShader) and args.stereo_sampler_ps:
        tree.stereo_sampler = args.stereo_sampler_ps
        if 's' in tree.reg_types and tree.stereo_sampler in tree.reg_types['s']:
            raise StereoSamplerAlreadyInUse(tree.stereo_sampler)
    elif 's' in tree.reg_types and tree.def_stereo_sampler in tree.reg_types['s']:
        # FIXME: There could be a few reasons for this. For now I assume the
        # shader was already using the sampler, but it's also possible we have
        # simply already added the stereo texture.

        tree.stereo_sampler = tree._find_free_reg('s', None)
        debug('WARNING: SHADER ALREADY USES %s! USING %s FOR STEREO SAMPLER INSTEAD!' % \
                (tree.def_stereo_sampler, tree.stereo_sampler))

        if isinstance(tree, VertexShader):
            acronym = 'VS'
            quirk = 257
        elif isinstance(tree, PixelShader):
            acronym = 'PS'
            quirk = 0
        else:
            raise AssertionError()

        if not hasattr(tree, 'ini'):
            tree.ini = []
        tree.ini.append(('Def%sSampler' % acronym,
            str(quirk + tree.stereo_sampler.num),
            'Shader already uses %s, so use %s instead:' % \
                (tree.def_stereo_sampler, tree.stereo_sampler)
            ))
    else:
        tree.stereo_sampler = tree.def_stereo_sampler

    if args.adjust_multiply and args.adjust_multiply != -1:
        w = args.adjust_multiply
    tree.stereo_const = tree._find_free_reg('c', None, desired = preferred_stereo_const)
    offset = 0
    offset += tree.insert_decl()
    offset += tree.insert_decl('def', [tree.stereo_const, x, y, z, w])
    if tree.stereo_sampler is not None:
        offset += tree.insert_decl('dcl_2d', [tree.stereo_sampler])
    offset += tree.insert_decl()
    return tree.stereo_const, offset

vanity_args = None
def vanity_comment(args, tree, what):
    global vanity_args

    if vanity_args is None:
        # Using a set here for *MASSIVE* performance boost over a list (e.g.
        # Life Is Strange has 75,000 pixel shaders hangs for minutes as a list,
        # as a set it's a fraction of a second)
        file_set = set(args.files)
        vanity_args = list(filter(lambda x: x not in file_set and '*' not in x, sys.argv[1:]))

    debug_verbose(0, " Applying: %s %s" % (what, tool_name))
    return [
        "%s DarkStarSword's %s:" % (what, tool_name),
        '%s %s' % (os.path.basename(sys.argv[0]), ' '.join(vanity_args + [tree.filename])),
    ]

def insert_vanity_comment(args, tree, where, what):
    off = 0
    off += tree.insert_instr(where + off)
    for comment in vanity_comment(args, tree, what):
        off += tree.insert_instr(where + off, comment = comment)
    return off

def append_vanity_comment(args, tree, what):
    tree.add_inst()
    for comment in vanity_comment(args, tree, what):
        tree.append(CPPStyleComment('// %s' % comment))
        tree.append(NewLine('\n'))

def output_texcoords(tree):
    for (t, r) in tree.declared:
        if t.startswith('dcl_texcoord') and r.startswith('o'):
            yield (t, r)

def find_declaration(tree, type, prefix = None):
    for (t, r) in tree.declared:
        if t == type:
            if prefix and not r.startswith(prefix):
                continue
            return r
    raise IndexError()

def adjust_ui_depth(tree, args):
    if not isinstance(tree, VS3):
        raise Exception('UI Depth adjustment must be done on a vertex shader')

    stereo_const, _ = insert_stereo_declarations(tree, args)

    pos_reg = tree._find_free_reg('r', VS3)
    tmp_reg = tree._find_free_reg('r', VS3, desired=31)
    dst_reg = find_declaration(tree, 'dcl_position', 'o').reg

    replace_regs = {dst_reg: pos_reg}
    tree.do_replacements(replace_regs, False)

    append_vanity_comment(args, tree, 'UI depth adjustment inserted with')
    if args.condition:
        tree.add_inst('mov', [tmp_reg.x, args.condition])
        tree.add_inst('if_eq', [tmp_reg.x, stereo_const.x])
    # TODO: Add support for --use-nv-stereo-reg-vs
    tree.add_inst('texldl', [tmp_reg, stereo_const.z, tree.stereo_sampler])
    separation = tmp_reg.x
    tree.add_inst('mad', [pos_reg.x, separation, args.adjust_ui_depth, pos_reg.x])
    if args.condition:
        tree.add_inst('endif', [])
    tree.add_inst('mov', [dst_reg, pos_reg])

def _adjust_output(tree, reg, args, stereo_const, tmp_reg):
    pos_reg = tree._find_free_reg('r', VS3)

    if reg.startswith('dcl_texcoord'):
        dst_reg = find_declaration(tree, reg, 'o').reg
    elif reg.startswith('texcoord') or reg == 'position':
        dst_reg = find_declaration(tree, 'dcl_%s' % reg, 'o').reg
    else:
        dst_reg = reg
    replace_regs = {dst_reg: pos_reg}
    tree.do_replacements(replace_regs, False)

    append_vanity_comment(args, tree, 'Output adjustment inserted with')
    if args.condition:
        tree.add_inst('mov', [tmp_reg.x, args.condition])
        tree.add_inst('if_eq', [tmp_reg.x, stereo_const.x])
    # TODO: Add support for --use-nv-stereo-reg-vs
    tree.add_inst('texldl', [tmp_reg, stereo_const.z, tree.stereo_sampler])
    separation = tmp_reg.x; convergence = tmp_reg.y
    tree.add_inst('add', [tmp_reg.w, pos_reg.w, -convergence])
    if not args.adjust_multiply:
        tree.add_inst('mad', [pos_reg.x, tmp_reg.w, separation, pos_reg.x])
    else:
        tree.add_inst('mul', [tmp_reg.w, tmp_reg.w, separation])
        if args.adjust_multiply and args.adjust_multiply != -1:
            tree.add_inst('mul', [tmp_reg.w, tmp_reg.w, stereo_const.w])
        if args.adjust_multiply and args.adjust_multiply == -1:
            tree.add_inst('add', [pos_reg.x, pos_reg.x, -tmp_reg.w])
        else:
            tree.add_inst('add', [pos_reg.x, pos_reg.x, tmp_reg.w])
    if args.condition:
        tree.add_inst('endif', [])
    tree.add_inst('mov', [dst_reg, pos_reg])

def adjust_output(tree, args):
    if not isinstance(tree, VS3):
        raise Exception('Output adjustment must be done on a vertex shader')

    stereo_const, _ = insert_stereo_declarations(tree, args)

    tmp_reg = tree._find_free_reg('r', VS3, desired=31)

    success = False

    for reg in args.adjust:
        try:
            _adjust_output(tree, reg, args, stereo_const, tmp_reg)
            success = True
        except Exception as e:
            if args.ignore_other_errors:
                collected_errors.append((tree.filename, e))
                import traceback, time
                traceback.print_exc()
                last_exc = e
                continue
            raise

    if not success and last_exc is not None:
        # We have already reported this exception, but since none of the inputs
        # were adjusted we don't want this shader to be installed. Raise a
        # special exception to skip it without double reporting the error.
        raise ExceptionDontReport()

def _adjust_input(tree, reg, args, stereo_const=None, tmp_reg=None, vanity="Input adjustment inserted with"):
    # TODO: Refactor common code with _adjust_output

    if stereo_const is None:
        stereo_const, _ = insert_stereo_declarations(tree, args)
    if tmp_reg is None:
        tmp_reg = tree._find_free_reg('r', VS3, desired=31)

    repl_reg = tree._find_free_reg('r', VS3, desired=30)

    if reg.startswith('dcl_texcoord'):
        declared_reg = find_declaration(tree, reg, 'v')
        org_reg = declared_reg.reg
    elif reg.startswith('texcoord'):
        declared_reg = find_declaration(tree, 'dcl_%s' % reg, 'v')
        org_reg = declared_reg.reg
    else:
        # FIXME: Look up mask in declaration
        declared_reg = org_reg = reg
    replace_regs = {org_reg: repl_reg}
    tree.do_replacements(replace_regs, False)

    repl_reg_mask = repl_reg
    if declared_reg is not None and declared_reg.swizzle is not None:
        repl_reg_mask = '%s.%s' % (repl_reg, declared_reg.swizzle)

    pos = tree.decl_end - 1
    pos += insert_vanity_comment(args, tree, pos, vanity)
    pos += tree.insert_instr(pos, NewInstruction('mov', [repl_reg_mask, org_reg]))
    if args.condition:
        pos += tree.insert_instr(pos, NewInstruction('mov', [tmp_reg.x, args.condition]))
        pos += tree.insert_instr(pos, NewInstruction('if_eq', [tmp_reg.x, stereo_const.x]))
    # TODO: Add support for --use-nv-stereo-reg-vs
    pos += tree.insert_instr(pos, NewInstruction('texldl', [tmp_reg, stereo_const.z, tree.stereo_sampler]))
    separation = tmp_reg.x; convergence = tmp_reg.y
    pos += tree.insert_instr(pos, NewInstruction('add', [tmp_reg.w, repl_reg.w, -convergence]))
    if not args.adjust_multiply:
        pos += tree.insert_instr(pos, NewInstruction('mad', [repl_reg.x, tmp_reg.w, separation, repl_reg.x]))
    else:
        pos += tree.insert_instr(pos, NewInstruction('mul', [tmp_reg.w, tmp_reg.w, separation]))
        if args.adjust_multiply and args.adjust_multiply != -1:
            pos += tree.insert_instr(pos, NewInstruction('mul', [tmp_reg.w, tmp_reg.w, stereo_const.w]))
        if args.adjust_multiply and args.adjust_multiply == -1:
            pos += tree.insert_instr(pos, NewInstruction('add', [repl_reg.x, repl_reg.x, -tmp_reg.w]))
        else:
            pos += tree.insert_instr(pos, NewInstruction('add', [repl_reg.x, repl_reg.x, tmp_reg.w]))
    if args.condition:
        pos += tree.insert_instr(pos, NewInstruction('endif', []))

def adjust_input(tree, args):
    stereo_const, _ = insert_stereo_declarations(tree, args)
    tmp_reg = tree._find_free_reg('r', VS3, desired=31)

    success = False

    for reg in args.adjust_input:
        try:
            _adjust_input(tree, reg, args, stereo_const, tmp_reg)
            success = True
        except Exception as e:
            if args.ignore_other_errors:
                collected_errors.append((tree.filename, e))
                import traceback, time
                traceback.print_exc()
                last_exc = e
                continue
            raise

    if not success and last_exc is not None:
        # We have already reported this exception, but since none of the inputs
        # were adjusted we don't want this shader to be installed. Raise a
        # special exception to skip it without double reporting the error.
        raise ExceptionDontReport()

def pos_to_line(tree, position):
    return len([ x for x in tree[:position] if isinstance(x, NewLine) ]) + 1

def prev_line_pos(tree, position):
    for p in range(position, -1, -1):
        if isinstance(tree[p], NewLine):
            return p + 1
    return 0

def next_line_pos(tree, position):
    for p in range(position, len(tree)):
        if isinstance(tree[p], NewLine):
            return p + 1
    return len(tree) + 1

def scan_shader(tree, reg, components=None, write=None, start=None, end=None, direction=1, stop=False, opcode=None):
    assert(direction == 1 or direction == -1)
    assert(write is not None)

    Match = collections.namedtuple('Match', ['line', 'token', 'instruction'])

    if opcode and not isinstance(opcode, (tuple, list)):
        opcode = (opcode, )

    if direction == 1:
        if start is None:
            start = 0
        if end is None:
            end = len(tree)
        def direction_iter(input):
            return input
    else:
        if start is None:
            start = len(tree) - 1
        if end is None:
            end = -1
        def direction_iter(input):
            return reversed(list(input))

    tmp = reg
    if components:
        if isinstance(components, str):
            tmp += '.%s' % components
        else:
            tmp += '.%s' % component_set_to_string(components)
    debug_verbose(1, "Scanning shader %s from line %i to %i for %s %s..." % (
            {1: 'downwards', -1: 'upwards'}[direction],
            pos_to_line(tree, start), pos_to_line(tree, end - direction),
            {True: 'write to', False: 'read from'}[write],
            tmp,
    ))

    if isinstance(components, str):
        components = set(components)

    def is_match(other):
        return other.reg == reg and \
                (not components or not other.swizzle or \
                components.intersection(set(list(other.swizzle))))
        # FIXME: Also handle implied destination mask from
        # instructions that only write to one component

    ret = []
    for i in range(start, end, direction):
        line = tree[i]
        if not isinstance(line, SyntaxTree):
            continue
        for (j, instr) in direction_iter(enumerate(line)):
            if not isinstance(instr, Instruction):
                continue
            if instr.is_def_or_dcl():
                continue
            if not instr.args:
                continue
            # debug('scanning %s' % instr)
            if opcode and instr.opcode not in opcode:
                continue
            if write:
                dest = instr.args[0]
                if is_match(dest):
                    debug_verbose(1, 'Found write to %s on line %s: %s' % (dest, pos_to_line(tree, i), instr))
                    ret.append(Match(i, j, instr))
                    if stop:
                        return ret
            else:
                for arg in instr.args[1:]:
                    if is_match(arg):
                        debug_verbose(1, 'Found read from %s on line %s: %s' % (arg, pos_to_line(tree, i), instr))
                        ret.append(Match(i, j, instr))
                        if stop:
                            return ret

    return ret

def find_row_major_matrix_multiply(tree, matrix_pattern, matrix_name):
    try:
        match = find_header(tree, matrix_pattern)
    except KeyError:
        debug_verbose(0, 'Shader does not use %s' % matrix_name)
        return None, None

    matrix = [ Register(match.group('constant')) ]
    for i in range(1,4):
        matrix.append(Register('c%i' % (matrix[0].num + i)))
    debug_verbose(0, '%s identified as %s-%s' % (matrix_name, matrix[0], matrix[3]))

    # XXX: Could optimise this by scanning around the preceeding hit, but
    # complicated by the fact the multiply order can vary. First instruction
    # should be a mul, 2nd/3rd should be mad, 4th should be mad/add, but since
    # the order of matrix registers can vary, just scan for all options and
    # verify later after sorting:
    results = []
    for i in range(4):
        result = scan_shader(tree, matrix[i], write=False, opcode=('mul', 'mad', 'add'))
        l = len(result)
        if l != 1:
            debug("Unsupported: %s[%i] referenced %i times" % (matrix_name, i, l))
            return None, None
        results.extend(result)

    results = sorted(results, key = lambda x: x.line)
    #print('\n'.join(map(repr, results)))

    if results[0].instruction.opcode != 'mul' \
    or results[1].instruction.opcode != 'mad' \
    or results[2].instruction.opcode != 'mad' \
    or results[3].instruction.opcode not in ('mad', 'add'):
        debug("Invalid matrix multiply flow: %s" % ' -> '.join([x.instruction.opcode for x in results]))
        return None, None

    for i in range(3):
        # Double check the intermediate register is not clobbered:
        if scan_shader(tree,
                results[i].instruction.args[0].reg,
                components = results[i].instruction.args[0].swizzle,
                write = True,
                start = results[i].line + 1,
                end = results[i+1].line):
            debug("Intermediate matrix multiply result clobbered")
            return None, None

        # Verify that the output from each instruction feeds into the next:
        if results[i].instruction.args[0].reg not in \
                [x.reg for x in results[i+1].instruction.args[1:]]:
            debug("Intermediate matrix multiply register not used in following instruction")
            return None, None

    return matrix, results

def asm_hlsl_swizzle(mask, swizzle):
    if mask and not swizzle:
        return mask
    swizzle4 = swizzle + swizzle[-1] * (4-len(swizzle))
    if not mask:
        return swizzle4
    ret = ''
    for component in mask:
        ret += {
            'x': swizzle4[0],
            'y': swizzle4[1],
            'z': swizzle4[2],
            'w': swizzle4[3],
        }[component]
    return ret

def follow_components_through_swizzle_and_mask(mask, swizzle):
    # Resolve the swizzle to HLSL style so the positions between mask and
    # swizzle will line up:
    resolved_swizzle = asm_hlsl_swizzle(mask, swizzle)
    components = []
    for component in 'xyzw':
        # Beware .find() can return -1, which is a valid list index!
        # Not sure why they didn't return None or raise an exception instead...
        # I reckon that there is a high probability that this design flaw has
        # led to exploitable programs out there in the wild...
        location = resolved_swizzle.find(component)
        components.append(location != -1 and mask[location] or None)
    return components

# Converts a set of components into a string, ensuring the order is consistently xyzw
def component_set_to_string(components):
    ret = ''
    if 'x' in components:
        ret += 'x'
    if 'y' in components:
        ret += 'y'
    if 'z' in components:
        ret += 'z'
    if 'w' in components:
        ret += 'w'
    return ret

def auto_fix_vertex_halo(tree, args):
    # This attempts to automatically fix vertex shaders that are broken in a
    # very common way, where the output position has been copied to a texcoord.
    # This is not a magic bullet - it can only fix fairly simple cases of this
    # type of broken shader, but may be useful for fixing halos, Unity surface
    # shaders, etc.

    if not isinstance(tree, VS3):
        raise Exception('Auto texcoord adjustment is only applicable to vertex shaders')

    # 1. Find output position variable from declarations
    pos_out = find_declaration(tree, 'dcl_position', 'o')

    # 2. Locate where in the shader the output position is set and note which
    #    temporary register was copied to it.
    results = scan_shader(tree, pos_out, components='xw', write=True)
    if not results:
        debug("Couldn't find write to output position register")
        return
    if len(results) > 1:
        # FUTURE: We may be able to handle certain cases of this
        debug_verbose(0, "Can't autofix a vertex shader writing to output position from multiple instructions")
        return
    (output_line, output_linepos, output_instr) = results[0]
    if output_instr.opcode != 'mov':
        debug_verbose(-1, 'Output not using mov instruction: %s' % output_instr)
        return
    temp_reg = output_instr.args[1]
    if not temp_reg.startswith('r'):
        debug_verbose(-1, 'Output not moved from a temporary register: %s' % output_instr)
        return

    # 3. Scan upwards to find where the X or W components of the temporary
    #    register was last set.
    results = scan_shader(tree, temp_reg.reg, components='xw', write=True, start=output_line - 1, direction=-1, stop=True)
    if not results:
        debug('WARNING: Output set from undefined register!!!?!')
        return
    (temp_reg_line, temp_reg_linepos, temp_reg_instr) = results[0]

    # 4. Scan between the two lines identified in 2 and 3 for any reads of the
    #    temporary register:
    results = scan_shader(tree, temp_reg.reg, write=False, start=temp_reg_line + 1, end=output_line)
    if results:
        # 5. If temporary register was read between temporary register being set
        #    and moved to output, relocate the output to just before the first
        #    line that read from the temporary register
        relocate_to = results[0][0]

        # 6. Scan for any writes to other components of the temporary register
        #    that we may have just moved the output register past, and copy
        #    these to the output position at the original output location.
        #    Bug fix - Only consider components that were originally output
        #    (caused issue on Dreamfall Chapters Speedtree fadeout in fog).
        output_components = set('yzw')
        if output_instr.args[0].swizzle is not None:
            output_components = set(output_instr.args[0].swizzle).intersection(output_components)

        results = scan_shader(tree, temp_reg.reg, components=output_components, write=True, start=relocate_to + 1, end=output_line)
        if results:
            components = [ tuple(instr.args[0].swizzle) for (_, _, instr) in results ]
            components = component_set_to_string(set(itertools.chain(*components)).intersection(output_components))
            tree.insert_instr(next_line_pos(tree, output_line))
            # Only apply components to destination (as mask) to avoid bugs like this one: "mov o6.yz, r1.yz"
            instr = NewInstruction('mov', ['%s.%s' % (pos_out.reg, components), temp_reg.reg])
            debug_verbose(-1, "Line %i: Inserting '%s'" % (pos_to_line(tree, output_line)+1, instr))
            tree.insert_instr(next_line_pos(tree, output_line), instr, 'Inserted by shadertool.py')

        # Actually do the relocation from 5 (FIXME: Move this up, being careful
        # of position offsets):
        line = tree[output_line]
        line.insert(0, CPPStyleComment('// '))
        line.append(WhiteSpace(' '))
        line.append(CPPStyleComment('// Relocated to line %i with shadertool.py' % pos_to_line(tree, relocate_to)))
        debug_verbose(-1, "Line %i: %s" % (pos_to_line(tree, output_line), tree[output_line]))
        tree.insert_instr(prev_line_pos(tree, output_line))
        debug_verbose(-1, "Line %i: Relocating '%s' to here" % (pos_to_line(tree, relocate_to), output_instr))
        relocate_to += tree.insert_instr(prev_line_pos(tree, relocate_to))
        tree.insert_instr(prev_line_pos(tree, relocate_to), output_instr, 'Relocated from line %i with shadertool.py' % pos_to_line(tree, output_line))
        output_line = relocate_to
    else:
        # 7. No reads above, scan downwards until temporary register X
        #    component is next set:
        results = scan_shader(tree, temp_reg.reg, components='x', write=True, start=output_line, stop=True)
        scan_until = len(tree)
        if results:
            scan_until = results[0].line

        # 8. Scan between the two lines identified by 2 and 7 for any reads of
        #    the temporary register:
        results = scan_shader(tree, temp_reg.reg, write=False, start=output_line + 1, end=scan_until, stop=True)
        if not results:
            debug_verbose(0, 'No other reads of temporary variable found, nothing to fix')
            return

    # 9. Insert stereo conversion after new location of move to output position.
    # FIXME: Refactor common code with the adjust_output, etc
    stereo_const, offset = insert_stereo_declarations(tree, args)
    pos = next_line_pos(tree, output_line + offset)
    t = tree._find_free_reg('r', VS3, desired=31)

    debug_verbose(-1, 'Line %i: Applying stereo correction formula to %s' % (pos_to_line(tree, pos), temp_reg.reg))
    pos += insert_vanity_comment(args, tree, pos, "Automatic vertex shader halo fix inserted with")

    if tree.stereo_sampler is not None:
        # Use Helix Mod stereo texture:
        pos += tree.insert_instr(pos, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
        separation = t.x; convergence = t.y
        pos += tree.insert_instr(pos, NewInstruction('add', [t.w, temp_reg.w, -convergence]))
        pos += tree.insert_instr(pos, NewInstruction('mad', [temp_reg.x, t.w, separation, temp_reg.x]))
    else:
        # Use undocumented register injected by 3D Vision driver:
        neg_sep_conv = tree.nv_stereo_reg.x; separation = tree.nv_stereo_reg.y
        pos += tree.insert_instr(pos, NewInstruction('mad', [temp_reg.x, temp_reg.w, neg_sep_conv, temp_reg.x]))
        pos += tree.insert_instr(pos, NewInstruction('add', [temp_reg.x, temp_reg.x, separation]))

    pos += tree.insert_instr(pos)

    tree.autofixed = True

def find_header(tree, comment_patterns):
    for line in range(tree.shader_start):
        for token in tree[line]:
            if not isinstance(token, CPPStyleComment):
                continue

            if not isinstance(comment_patterns, (tuple, list)):
                comment_patterns = (comment_patterns, )

            for comment_pattern in comment_patterns:
                match = comment_pattern.match(token)
                if match is not None:
                    return match
    raise KeyError()

def find_texture(tree, comment_patterns):
    match = find_header(tree, comment_patterns)
    return Register('s' + match.group('texture'))

def find_const(tree, comment_patterns):
    match = find_header(tree, comment_patterns)
    # XXX: In some of these below I added 'c' manually - double check the
    # patterns before switching anything to use this.
    return Register(match.group('constant'))

def disable_unreal_correction(tree, args, redundant_check):
    # In Life Is Strange I found a lot of Unreal Engine shaders are now using
    # the vPos semantic, and then applying a stereo correction on top of that,
    # which is wrong as vPos is already the correct screen location.

    if not isinstance(tree, PS3):
        raise Exception('Disabling redundant Unreal correction is only applicable to pixel shaders')

    if redundant_check:
        try:
            vPos = find_declaration(tree, 'dcl', 'vPos.xy')
        except IndexError:
            debug_verbose(0, 'Shader does not use vPos')
            return False

    try:
        match = find_header(tree, unreal_NvStereoEnabled_pattern)
    except KeyError:
        debug_verbose(0, 'Shader does not use NvStereoEnabled')
        return False

    constant = Register(match.group('constant'))
    debug_verbose(-1, 'Disabling NvStereoEnabled %s' % constant)

    if redundant_check:
        tree.decl_end += insert_vanity_comment(args, tree, tree.decl_end, "Redundant Unreal Engine stereo correction disabled by")
    else:
        tree.decl_end += insert_vanity_comment(args, tree, tree.decl_end, "Unreal Engine stereo correction disabled by")
    tree.insert_decl('def', [constant, 0, 0, 0, 0], comment='Overrides NvStereoEnabled passed from Unreal Engine')
    tree.insert_decl()

    tree.autofixed = True

    return True

def auto_fix_unreal_light_shafts(tree, args):
    if not isinstance(tree, PS3):
        raise Exception('Unreal light shaft auto fix is only applicable to pixel shaders')

    try:
        match = find_header(tree, unreal_TextureSpaceBlurOrigin_pattern)
    except KeyError:
        debug_verbose(0, 'Shader does not use TextureSpaceBlurOrigin')
        return

    orig = Register(match.group('constant'))
    debug_verbose(0, 'TextureSpaceBlurOrigin identified as %s' % orig)

    results = scan_shader(tree, orig, write=False)
    if not results:
        debug_verbose(0, 'TextureSpaceBlurOrigin is not used in shader')
        return

    debug_verbose(-1, 'Applying Unreal Engine 3 light shaft fix')

    adj = tree._find_free_reg('r', PS3)
    t = tree._find_free_reg('r', PS3, desired=31)
    stereo_const, _ = insert_stereo_declarations(tree, args, w = 0.5)

    replace_regs = {orig: adj}
    tree.do_replacements(replace_regs, False)

    pos = tree.decl_end
    pos += insert_vanity_comment(args, tree, tree.decl_end, "Unreal light shaft fix inserted with")
    pos += tree.insert_instr(pos, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
    pos += tree.insert_instr(pos, NewInstruction('mov', [adj, orig]), comment='TextureSpaceBlurOrigin')
    pos += tree.insert_instr(pos, NewInstruction('mad', [adj.x, t.x, stereo_const.w, adj.x]), comment='Adjust each eye by 1/2 separation')
    pos += tree.insert_instr(pos)

    tree.autofixed = True

def adjust_simple_reflection_to_infinity(tree, args, sampler_pattern, sampler_name, fix_name="simple reflection infinity adjustment"):
    if not isinstance(tree, PS3):
        raise Exception('%s fix is only applicable to pixel shaders' % fix_name)

    try:
        match = find_header(tree, sampler_pattern)
    except KeyError:
        debug_verbose(0, 'Shader does not use %s' % sampler_name)
        return

    orig = Register('s' + match.group('texture'))
    debug_verbose(0, '%s identified as %s' % (sampler_name, orig))

    results = scan_shader(tree, orig, write=False)
    if not results:
        debug_verbose(0, '%s is not used in shader' % sampler_name)
        return
    if len(results) > 1:
        debug("Autofixing a shader using %s multiple times is untested and disabled for safety. Please enable it, test and report back." % sampler_name)
        return

    debug_verbose(-1, 'Applying simple adjustment to move reflection to infinity')

    t = tree._find_free_reg('r', PS3, desired=31)
    stereo_const, offset = insert_stereo_declarations(tree, args, w = 0.5)

    for (sampler_line, sampler_linepos, sampler_instr) in results:
        orig_pos = pos = prev_line_pos(tree, sampler_line + offset)
        reg = sampler_instr.args[1]
        pos += insert_vanity_comment(args, tree, pos, "%s fix inserted with" % sampler_name)
        pos += tree.insert_instr(pos, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
        pos += tree.insert_instr(pos, NewInstruction('mad', [reg.x, -t.x, stereo_const.w, reg.x]))
        pos += tree.insert_instr(pos)
        offset += pos - orig_pos

        tree.autofixed = True

# Not sure if this is a generic UE3 thing, or specific to Life Is Strange
def auto_fix_unreal_dne_reflection(tree, args):
    return adjust_simple_reflection_to_infinity(tree, args, unreal_DNEReflectionTexture_pattern, 'DNEReflectionTexture')

def adjust_unity_ceto_reflections(tree, args):
    return adjust_simple_reflection_to_infinity(tree, args, unity_Ceto_Reflections, 'Ceto_Reflections')

def auto_fix_unreal_shadows(tree, args, pattern=unreal_ScreenToShadowMatrix_pattern, matrix_name='ScreenToShadowMatrix', name="shadow"):
    if not isinstance(tree, PS3):
        raise Exception('Unreal %s auto fix is only applicable to pixel shaders' % name)

    try:
        match = find_header(tree, pattern)
    except KeyError:
        debug_verbose(0, 'Shader does not use %s' % matrix_name)
        return

    matrix0 = Register(match.group('constant'))
    matrix2 = Register('c%i' % (matrix0.num + 2))
    debug_verbose(0, '%s identified as %s %s' % (matrix_name, matrix0, matrix2))

    results0 = scan_shader(tree, matrix0, write=False)
    results2 = scan_shader(tree, matrix2, write=False)
    if not results0 or not results2:
        debug_verbose(0, '%s is not used in shader' % matrix_name)
        return
    if len(results0) > 1 or len(results2) > 1:
        debug("Autofixing a shader using %s multiple times is untested and disabled for safety. Please enable it, test and report back." % matrix_name)
        return

    (x_line, x_linepos, x_instr) = results0[0]
    (z_line, z_linepos, z_instr) = results2[0]

    if x_instr.opcode != 'mad' or z_instr.opcode != 'mad':
        debug('%s used in an unexpected way (column-major/row-major?)' % matrix_name)
        return

    if x_instr.args[1].reg == matrix0:
        x_reg = x_instr.args[2]
    elif x_instr.args[2].reg == matrix0:
        x_reg = x_instr.args[1]
    else:
        debug('%s[0] used in an unexpected way' % matrix_name)
        return

    if z_instr.args[1].reg == matrix2:
        w_reg = z_instr.args[2]
    elif z_instr.args[2].reg == matrix2:
        w_reg = z_instr.args[1]
    else:
        debug('%s[2] used in an unexpected way' % matrix_name)
        return

    debug_verbose(-1, 'Applying Unreal Engine 3 %s fix' % name)

    try:
        vPos = find_declaration(tree, 'dcl', 'vPos.xy')
    except IndexError:
        vPos = None

    t = tree._find_free_reg('r', PS3, desired=31)
    stereo_const, offset = insert_stereo_declarations(tree, args, w = 0.5)
    if vPos is None:
        texcoord = find_declaration(tree, 'dcl_texcoord', 'v')

        mask = ''
        if texcoord.swizzle:
            mask = '.%s' % texcoord.swizzle
        texcoord_adj = tree._find_free_reg('r', PS3)

        replace_regs = {texcoord.reg: texcoord_adj}
        tree.do_replacements(replace_regs, False)

    orig_offset = tree.decl_end
    vanity_inserted = disable_unreal_correction(tree, args, False)

    pos = tree.decl_end
    if vPos is None:
        if not vanity_inserted:
            pos += insert_vanity_comment(args, tree, tree.decl_end, "Unreal Engine %s fix inserted with" % name)
        pos += tree.insert_instr(pos, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
        pos += tree.insert_instr(pos, NewInstruction('mov', [texcoord_adj + mask, texcoord.reg]))
        pos += tree.insert_instr(pos, NewInstruction('add', [t.w, texcoord_adj.w, -t.y]))
        pos += tree.insert_instr(pos, NewInstruction('mad', [texcoord_adj.x, t.w, t.x, texcoord_adj.x]))
        pos += tree.insert_instr(pos)
    offset += pos - orig_offset

    line = min(x_line, z_line)
    orig_pos = pos = prev_line_pos(tree, line + offset)
    pos += insert_vanity_comment(args, tree, pos, "Unreal Engine %s fix inserted with" % name)
    pos += tree.insert_instr(pos, NewInstruction('add', [t.w, w_reg, -t.y]))
    pos += tree.insert_instr(pos, NewInstruction('mad', [x_reg, -t.w, t.x, x_reg]))
    pos += tree.insert_instr(pos)
    offset += pos - orig_pos

    tree.autofixed = True

def auto_fix_unreal_lights(tree, args):
    return auto_fix_unreal_shadows(tree, args, unreal_ScreenToLight_pattern, matrix_name='ScreenToLight', name="light")

def fix_unreal_halo_vpm(tree, args):
    if not isinstance(tree, PS3):
        raise Exception('Unreal ViewProjectionMatrix halo fix is only applicable to pixel shaders')

    matrix, results = find_row_major_matrix_multiply(tree, unreal_ViewProjectionMatrix_pattern, 'ViewProjectionMatrix')
    if matrix is None:
        return

    debug_verbose(-1, 'Applying Unreal Engine 3 ViewProjectionMatrix halo fix')

    # Find correct swizzle for clip space register, e.g.
    # mad r2.xyz, c11.xyww, v3.w, r2 --> r2.x, r2.z
    # mad r7.yzw, c11.xxyw, v7.w, r2 --> r7.y, r7.w
    (line, linepos, instr) = results[3]
    components = follow_components_through_swizzle_and_mask(instr.args[0].swizzle, instr.args[1].swizzle)
    if components[0] is None or components[3] is None:
        debug_verbose(0, 'VPM multiplication does not use both x and w components')
        return None
    clip_reg_x = Register('%s.%s' % (instr.args[0].reg, components[0]))
    clip_reg_w = Register('%s.%s' % (instr.args[0].reg, components[3]))

    t = tree._find_free_reg('r', PS3, desired=31)
    stereo_const, offset = insert_stereo_declarations(tree, args, w = 0.5)

    pos = next_line_pos(tree, line + offset)

    pos += insert_vanity_comment(args, tree, pos, "Unreal Engine ViewProjectionMatrix halo fix inserted with")
    pos += tree.insert_instr(pos, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
    separation = t.x; convergence = t.y
    pos += tree.insert_instr(pos, NewInstruction('add', [t.w, clip_reg_w, -convergence]))
    pos += tree.insert_instr(pos, NewInstruction('mad', [clip_reg_x, t.w, separation, clip_reg_x]))
    pos += tree.insert_instr(pos)

    tree.autofixed = True

def fix_unreal_halo_sct(tree, args):
    # These halos could be fixed in the vertex shader using the generic vertex
    # shader halo fix, but doing so may break other effects in UE3 games, so
    # better to detect when the fix is required in the pixel shader instead.

    if not isinstance(tree, PS3):
        raise Exception('Unreal SceneColorTexture halo fix is only applicable to pixel shaders')

    try:
        SceneColorTexture = find_texture(tree, unreal_SceneColorTexture_pattern)
        ScreenPositionScaleBias = find_const(tree, unreal_ScreenPositionScaleBias_pattern)
    except KeyError:
        debug_verbose(0, 'Shader does not use SceneColorTexture and/or ScreenPositionScaleBias')
        return

    debug_verbose(0, 'SceneColorTexture identified as %s' % SceneColorTexture)
    debug_verbose(0, 'ScreenPositionScaleBias identified as %s' % ScreenPositionScaleBias)

    results = scan_shader(tree, SceneColorTexture, write=False)
    if not results:
        debug_verbose(0, 'SceneColorTexture is not used in shader')
        return
    if len(results) > 1:
        debug("Autofixing a shader using SceneColorTexture multiple times is unsupported.")
        return
    (line, linepos, instr) = results[0]

    # Trace back to mad rX.xy, rM.xy, cN, cN.wzzw instruction:
    results = scan_shader(tree, instr.args[1].reg, components='x', write=True, start=line - 1, direction=-1, stop=True)
    assert(len(results) == 1)
    (line, linepos, instr) = results[0]
    if instr.opcode != 'mad':
        debug("Unexpected instruction %s, aborting" % instr)
        return
    if [x.reg for x in instr.args[1:]].count(ScreenPositionScaleBias) != 2:
        debug("Unexpected flow - instruction did not use ScreenPositionScaleBias, aborting")
        return

    # Trace back to input register:
    reg = [x for x in instr.args[1:3] if x.reg.startswith('r')]
    assert(len(reg) == 1)
    reg = reg[0]
    swizzle = asm_hlsl_swizzle(instr.args[0].swizzle, reg.swizzle)
    results = scan_shader(tree, reg.reg, components=swizzle, write=True, start=line - 1, direction=-1, stop=True)
    (line, linepos, instr) = results[0]
    if instr.opcode != 'mul':
        debug("Unexpected instruction %s, aborting" % instr)
        return
    vreg = [x for x in instr.args[1:] if x.reg.startswith('v')]
    if len(vreg) != 1:
        debug("Unable to determine input register")
        return
    vreg = vreg[0]

    # Could trace further and verify that this was multiplied by 1/vreg.w, but
    # this is probably fine

    debug_verbose(-1, 'Applying Unreal Engine 3 SceneColorTexture halo fix')
    _adjust_input(tree, vreg, args, vanity="Unreal Engine SceneColorTexture halo fix inserted with")

    tree.autofixed = True


def fix_unity_lighting_ps(tree, args):
    if not isinstance(tree, PS3):
        # We could potentially fix the vertex shaders as well, but since there
        # is only a few of them and they sometimes need custom adjustments,
        # it's easier to just use a template (check my 3d-fixes repository) and
        # tweak as necessary.
        raise Exception('Unity lighting adjustment is only applicable to pixel shaders. Check my templates for the corresponding vertex shader & DX9Settings.ini!')

    try:
        match = find_header(tree, unity_CameraToWorld)
    except KeyError:
        debug_verbose(0, 'Shader does not use _CameraToWorld, or is missing headers (my other scripts can extract these)')
        return
    _CameraToWorld0 = Register('c' + match.group('matrix'))
    _CameraToWorld1 = Register('c%i' % (_CameraToWorld0.num + 1))
    _CameraToWorld2 = Register('c%i' % (_CameraToWorld0.num + 2))

    try:
        match = find_header(tree, unity_WorldSpaceCameraPos)
    except KeyError:
        debug_verbose(0, 'Shader does not use _WorldSpaceCameraPos - skipping environment/specular adjustment')
        _WorldSpaceCameraPos = None
    else:
        _WorldSpaceCameraPos = Register('c' + match.group('constant'))

    try:
        match = find_header(tree, unity_ZBufferParams)
    except KeyError:
        debug_verbose(0, 'Shader does not use _ZBufferParams, or is missing headers (my other scripts can extract these)')
        return
    _ZBufferParams = Register('c' + match.group('constant'))

    debug_verbose(0, '_CameraToWorld in %s, _ZBufferParams in %s' % (_CameraToWorld0, _ZBufferParams))

    # Find _CameraToWorld usage - adjustment must be above this point, and this
    # gives us the register with X that needs to be adjusted:
    results = scan_shader(tree, _CameraToWorld0, write=False, opcode=('dp4', 'dp4_pp'))
    if len(results) != 1:
        debug_verbose(0, '_CameraToWorld read from %i instructions (only exactly 1 read currently supported)' % len(results))
        return
    (_CameraToWorld_line, linepos, instr) = results[0]

    if instr.args[1] == _CameraToWorld0:
        reg = instr.args[2]
    elif instr.args[2] == _CameraToWorld0:
        reg = instr.args[1]
    else:
        assert(False)
    if reg.swizzle:
        x_reg = Register('%s.%s' % (reg.reg, reg.swizzle[0]))
    else:
        x_reg = Register('%s.x' % reg.reg)

    # And once more to find register with Z to use as depth (new approach as of
    # Dreamfall Chapters Unity 5, we still check some of the code flow to have
    # a higher degree of confidence that this is a lighting shader):
    results = scan_shader(tree, _CameraToWorld2, write=False, opcode=('dp4', 'dp4_pp'))
    if len(results) != 1:
        debug_verbose(0, '_CameraToWorld read from %i instructions (only exactly 1 read currently supported)' % len(results))
        return
    (line, linepos, instr) = results[0]

    if instr.args[1] == _CameraToWorld2:
        reg = instr.args[2]
    elif instr.args[2] == _CameraToWorld2:
        reg = instr.args[1]
    else:
        assert(False)
    if reg.swizzle:
        depth = Register('%s.%s' % (reg.reg, reg.swizzle[2]))
    else:
        depth = Register('%s.z' % reg.reg)

    line = tree[line]
    line.append(WhiteSpace(' '))
    line.append(CPPStyleComment('// depth in %s' % depth))

    # Find _ZBufferParams usage to find where depth is sampled (could use
    # _CameraDepthTexture, but that takes an extra step and more can go wrong)
    results = scan_shader(tree, _ZBufferParams, write=False, end=_CameraToWorld_line-1)
    if len(results) != 2:
        debug_verbose(0, '_ZBufferParams read %i times (only exactly 2 reads currently supported)' % len(results))
        return
    (line, linepos, instr) = results[0]
    if results[1].line != line:
        debug_verbose(0, '_ZBufferParams read from two different instructions' % len(results))
        return
    reg = instr.args[0]

    # We're expecting a reciprocal calculation as part of the Z buffer -> world
    # Z scaling:
    results = scan_shader(tree, reg.reg, components=reg.swizzle, opcode='rcp',
            write=False, start=line+1, end=_CameraToWorld_line-1, stop=True)
    if not results:
        debug_verbose(0, 'Could not find expected rcp instruction')
        return
    (line, linepos, instr) = results[0]
    reg = instr.args[0]

    # Find where the reciprocal is next used:
    results = scan_shader(tree, reg.reg, components=reg.swizzle, write=False,
            start=line+1, end=_CameraToWorld_line-1, stop=True)
    if not results:
        debug_verbose(0, 'Could not find expected instruction')
        return
    (line, linepos, instr) = results[0]
    reg = instr.args[0]

    # We used to trace the function forwards more here, but Dreamfall Chapters
    # got complicated after the Unity 5 update. Now we find the depth from the
    # matrix multiply instead, which hopefully should be more robust.

    # If we ever need the old procedure, it's in the git history.

    t = tree._find_free_reg('r', PS3, desired=31)
    texcoord = tree._find_free_reg('v', PS3, desired=5)
    offset = tree.insert_decl()
    offset += tree.insert_decl('dcl_texcoord5', ['%s.x' % texcoord], comment='New input from vertex shader with unity_CameraInvProjection[0].x')
    stereo_const, offset2 = insert_stereo_declarations(tree, args, w = 0.5)
    offset += offset2

    pos = tree.decl_end
    pos += tree.insert_instr(pos, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
    separation = t.x; convergence = t.y

    # Apply a stereo correction to the world space camera position - this
    # pushes environment reflections, specular highlights, etc to the correct
    # depth in Unity 5. Skip adjustment for Unity 4 style shaders that don't
    # use the world space camera position:
    if _WorldSpaceCameraPos is not None:
        repl_cam_pos = tree._find_free_reg('r', PS3, desired=30)
        view_space_adj = tree._find_free_reg('r', PS3, desired=29)
        world_space_adj = tree._find_free_reg('r', PS3, desired=28)
        replace_regs = {_WorldSpaceCameraPos: repl_cam_pos}
        tree.do_replacements(replace_regs, False)
        pos += insert_vanity_comment(args, tree, pos, "Unity reflection/specular fix inserted with")
        pos += tree.insert_instr(pos, NewInstruction('mov', [repl_cam_pos, _WorldSpaceCameraPos]))
        pos += tree.insert_instr(pos, NewInstruction('mov', [view_space_adj, tree.stereo_const.x]))
        pos += tree.insert_instr(pos, NewInstruction('mul', [view_space_adj.x, separation, -convergence]))
        pos += tree.insert_instr(pos, NewInstruction('mul', [view_space_adj.x, view_space_adj.x, texcoord.x]))
        pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.x, _CameraToWorld0, view_space_adj]))
        pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.y, _CameraToWorld1, view_space_adj]))
        pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.z, _CameraToWorld2, view_space_adj]))
        pos += tree.insert_instr(pos, NewInstruction('add', [repl_cam_pos.xyz, repl_cam_pos, -world_space_adj]))

    pos += tree.insert_instr(pos)
    offset += pos - tree.decl_end

    pos = _CameraToWorld_line + offset
    debug_verbose(-1, 'Line %i: Applying Unity pixel shader light/shadow fix. depth in %s, x in %s' % (pos_to_line(tree, pos), depth, x_reg))
    pos += insert_vanity_comment(args, tree, pos, "Unity light/shadow fix (pixel shader stage) inserted with")

    pos += tree.insert_instr(pos, NewInstruction('add', [t.w, depth, -convergence]))
    pos += tree.insert_instr(pos, NewInstruction('mul', [t.w, t.w, separation]))
    pos += tree.insert_instr(pos, NewInstruction('mad', [x_reg.x, -t.w, texcoord.x, x_reg.x]))
    pos += tree.insert_instr(pos)

    tree.autofixed = True

def fix_unity_lighting_ps_world(tree, args):
    if not isinstance(tree, PS3):
        # We could potentially fix the vertex shaders as well, but since there
        # is only a few of them and they sometimes need custom adjustments,
        # it's easier to just use a template (check my 3d-fixes repository) and
        # tweak as necessary.
        raise Exception('Unity lighting adjustment is only applicable to pixel shaders. Check my templates for the corresponding vertex shader & DX9Settings.ini!')

    # FIXME: Refactor with fix_unity_reflection
    inv_mvp0 = tree._find_free_reg('c', PS3, desired=180)
    # FIXME: Confirm these are free too:
    inv_mvp1 = Register('c%i' % (inv_mvp0.num + 1))
    inv_mvp2 = Register('c%i' % (inv_mvp0.num + 2))
    inv_mvp3 = Register('c%i' % (inv_mvp0.num + 3))
    _Object2World0 = tree._find_free_reg('c', PS3, desired=190)
    # FIXME: Confirm these are free too:
    _Object2World1 = Register('c%i' % (_Object2World0.num + 1))
    _Object2World2 = Register('c%i' % (_Object2World0.num + 2))

    try:
        match = find_header(tree, unity_CameraToWorld)
    except KeyError:
        debug_verbose(0, 'Shader does not use _CameraToWorld, or is missing headers (my other scripts can extract these)')
        return
    _CameraToWorld0 = Register('c' + match.group('matrix'))
    _CameraToWorld1 = Register('c%i' % (_CameraToWorld0.num + 1))
    _CameraToWorld2 = Register('c%i' % (_CameraToWorld0.num + 2))

    debug_verbose(0, '_CameraToWorld in %s' % _CameraToWorld0)

    try:
        match = find_header(tree, unity_WorldSpaceCameraPos)
    except KeyError:
        debug_verbose(0, 'Shader does not use _WorldSpaceCameraPos - skipping environment/specular adjustment')
        _WorldSpaceCameraPos = None
    else:
        _WorldSpaceCameraPos = Register('c' + match.group('constant'))

    # XXX: Directional lighting shaders seem to have a bogus _ZBufferParams!
    try:
        match = find_header(tree, unity_headers_attached)
    except KeyError:
        debug('Skipping possible depth buffer source - shader does not have Unity headers attached so unable to check what kind of lighting shader it is')
        has_unity_headers = False
    else:
        has_unity_headers = True
        try:
            match = find_header(tree, unity_shader_directional_lighting)
        except KeyError:
            try:
                match = find_header(tree, unity_ZBufferParams)
            except KeyError:
                debug_verbose(0, 'Shader does not use _ZBufferParams')
                return
            _ZBufferParams = Register('c' + match.group('constant'))

            try:
                match = find_header(tree, unity_CameraDepthTexture)
            except KeyError:
                debug_verbose(0, 'Shader does not use _CameraDepthTexture')
                return
            _CameraDepthTexture = Register('s' + match.group('texture'))
        else:
            _CameraDepthTexture = _ZBufferParams = None

    # Find _CameraToWorld usage - adjustment must be below this point, and this
    # gives us the register with X that needs to be adjusted:
    results = scan_shader(tree, _CameraToWorld0, write=False, opcode=('dp4', 'dp4_pp'))
    if len(results) != 1:
        debug_verbose(0, '_CameraToWorld read from %i instructions (only exactly 1 read currently supported)' % len(results))
        return
    (_CameraToWorld_line, linepos, instr) = results[0]

    if instr.args[0].swizzle != 'x':
        raise Exception('FIXME: Destination of _CameraToWorld[0] not in simple form')
    world_coord = Register(instr.args[0].reg)

    results = scan_shader(tree, _CameraToWorld1, write=False, opcode=('dp4', 'dp4_pp'))
    if len(results) != 1:
        debug_verbose(0, '_CameraToWorld read from %i instructions (only exactly 1 read currently supported)' % len(results))
        return
    (line, linepos, instr) = results[0]
    if  world_coord.reg != instr.args[0].reg or instr.args[0].swizzle != 'y':
        raise Exception('FIXME: Destination of _CameraToWorld[1] not in simple form')
    if (line > _CameraToWorld_line):
        (_CameraToWorld_line, linepos, instr) = (line, linepos, instr)

    # And once more to find register with Z to use as depth (new approach as of
    # Dreamfall Chapters Unity 5, we still check some of the code flow to have
    # a higher degree of confidence that this is a lighting shader):
    results = scan_shader(tree, _CameraToWorld2, write=False, opcode=('dp4', 'dp4_pp'))
    if len(results) != 1:
        debug_verbose(0, '_CameraToWorld read from %i instructions (only exactly 1 read currently supported)' % len(results))
        return
    (line, linepos, instr) = results[0]
    if  world_coord.reg != instr.args[0].reg or instr.args[0].swizzle != 'z':
        raise Exception('FIXME: Destination of _CameraToWorld[2] not in simple form')
    if (line > _CameraToWorld_line):
        (_CameraToWorld_line, linepos, instr) = (line, linepos, instr)

    if instr.args[1] == _CameraToWorld2:
        reg = instr.args[2]
    elif instr.args[2] == _CameraToWorld2:
        reg = instr.args[1]
    else:
        assert(False)
    if reg.swizzle:
        depth = Register('%s.%s' % (reg.reg, reg.swizzle[2]))
    else:
        depth = Register('%s.z' % reg.reg)

    line = tree[line]
    line.append(WhiteSpace(' '))
    line.append(CPPStyleComment('// depth in %s' % depth))

    t = tree._find_free_reg('r', PS3, desired=31)
    stereo_const, offset = insert_stereo_declarations(tree, args, w = 0.5)

    pos = tree.decl_end
    pos += tree.insert_instr(pos, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
    separation = t.x; convergence = t.y

    clip_space_adj = tree._find_free_reg('r', PS3, desired=29)
    local_space_adj = tree._find_free_reg('r', PS3, desired=28)
    world_space_adj = clip_space_adj # Reuse the same register

    # Apply a stereo correction to the world space camera position - this
    # pushes environment reflections, specular highlights, etc to infinity in
    # Unity 5. Skip adjustment for Unity 4 style shaders that don't use the
    # world space camera position:
    if _WorldSpaceCameraPos is not None:
        repl_cam_pos = tree._find_free_reg('r', PS3, desired=30)
        replace_regs = {_WorldSpaceCameraPos: repl_cam_pos}
        tree.do_replacements(replace_regs, False)
        pos += insert_vanity_comment(args, tree, pos, "Unity reflection/specular fix inserted with")
        pos += tree.insert_instr(pos, NewInstruction('mov', [repl_cam_pos, _WorldSpaceCameraPos]))
        pos += tree.insert_instr(pos, NewInstruction('mov', [clip_space_adj, tree.stereo_const.x]))
        pos += tree.insert_instr(pos, NewInstruction('mul', [clip_space_adj.x, separation, -convergence]))
        pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.x, inv_mvp0, clip_space_adj]))
        pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.y, inv_mvp1, clip_space_adj]))
        pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.z, inv_mvp2, clip_space_adj]))
        pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.w, inv_mvp3, clip_space_adj]))
        pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.x, _Object2World0, local_space_adj]))
        pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.y, _Object2World1, local_space_adj]))
        pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.z, _Object2World2, local_space_adj]))
        pos += tree.insert_instr(pos, NewInstruction('add', [repl_cam_pos.xyz, repl_cam_pos, -world_space_adj]))

    pos += tree.insert_instr(pos)
    offset += pos - tree.decl_end

    pos = _CameraToWorld_line + offset + 2
    debug_verbose(-1, 'Line %i: Applying Unity pixel shader light/shadow fix (world-space variant). depth in %s, world_coord in %s' % (pos_to_line(tree, pos), depth, world_coord))
    pos += insert_vanity_comment(args, tree, pos, "Unity light/shadow fix (pixel shader stage, world-space variant) inserted with")
    pos += tree.insert_instr(pos, NewInstruction('mov', [clip_space_adj, tree.stereo_const.x]))
    pos += tree.insert_instr(pos, NewInstruction('add', [clip_space_adj.x, depth, -convergence]))
    pos += tree.insert_instr(pos, NewInstruction('mul', [clip_space_adj.x, clip_space_adj.x, separation]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.x, inv_mvp0, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.y, inv_mvp1, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.z, inv_mvp2, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.w, inv_mvp3, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.x, _Object2World0, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.y, _Object2World1, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.z, _Object2World2, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('add', [world_coord.xyz, world_coord, -world_space_adj]))
    pos += tree.insert_instr(pos)

    if not hasattr(tree, 'ini'):
        tree.ini = []
    tree.ini.append(('UseMatrix', 'true',
        'Copy inversed MVP matrix and _Object2World matrix in for world-space variant of Unity lighting fix'))
    tree.ini.append(('MatrixReg', str(inv_mvp0.num), None))
    tree.ini.append(('UseMatrix1', 'true', None))
    tree.ini.append(('MatrixReg1', str(_Object2World0.num), None))
    if has_unity_headers:
        if _CameraDepthTexture is not None:
            tree.ini.append(('GetSampler1FromReg', str(_CameraDepthTexture.num),
                'Copy _CameraDepthTexture and _ZBufferParams for auto HUD adjustment'))
            tree.ini.append(('GetConst1FromReg', str(_ZBufferParams.num), None))
        else:
            tree.ini.append((None, None, 'Directional lighting shader, _ZBufferParams may be bogus'))
    else:
            tree.ini.append((None, None, 'No Unity headers attached, skipping possible depth buffer source'))

    tree.autofixed = True


# FIXME: This is for the output to the pixel shaders, but there's no guarantee
# it (or potentially any other) texcoord will be free.  For now just bail if
# it's taken, but we might need to come up with a more robust solution (could
# allocate a different texcoord, but need to make sure the pixel shaders use
# the same one. Could possibly copy both matrices to the pixel shaders with
# Helix Mod, but we might already be using the copy slots for other purposes,
# like fixing the lighting, which is much more important). We will write a
# special value to the otherwise unused W component of the output to denote
# that it is from the vertex shader - in case we end up running a modified
# pixel shader with an original vertex shader.
WorldSpaceCameraPosTexcoord = 'dcl_texcoord8'
WorldSpaceCameraPosMagicW = 1337

# FIXME: Refactor, or maybe just drop altogether since there is a more
# universal version now
def fix_unity_reflection_vs_variant_vs(tree, args):
    try:
        match = find_header(tree, unity_WorldSpaceCameraPos)
    except KeyError:
        debug_verbose(0, 'Shader does not use _WorldSpaceCameraPos')
        return
    _WorldSpaceCameraPos = Register('c' + match.group('constant'))

    inv_mvp0 = tree._find_free_reg('c', VS3, desired=180)
    # FIXME: Confirm these are free too:
    inv_mvp1 = Register('c%i' % (inv_mvp0.num + 1))
    inv_mvp2 = Register('c%i' % (inv_mvp0.num + 2))
    inv_mvp3 = Register('c%i' % (inv_mvp0.num + 3))

    try:
        match = find_header(tree, unity_Object2World)
    except KeyError:
        debug_verbose(0, 'Shader does not use _Object2World, or is missing headers (my other scripts can extract these)')
        return
    _Object2World0 = Register('c' + match.group('matrix'))
    _Object2World1 = Register('c%i' % (_Object2World0.num + 1))
    _Object2World2 = Register('c%i' % (_Object2World0.num + 2))

    try:
        match = find_header(tree, unity_glstate_matrix_mvp_pattern)
    except KeyError:
        debug_verbose(0, 'Shader does not use glstate_matrix_mvp')
        return
    unity_glstate_matrix_mvp = Register('c' + match.group('matrix'))

    try:
        d = find_declaration(tree, WorldSpaceCameraPosTexcoord)
    except:
        pass
    else:
        debug_verbose(0, 'Shader already uses %s' % WorldSpaceCameraPosTexcoord)
        raise NoFreeRegisters(WorldSpaceCameraPosTexcoord)

    t = tree._find_free_reg('r', VS3, desired=31)
    adj_output = tree._find_free_reg('o', VS3)
    tree.insert_decl()
    tree.insert_decl(WorldSpaceCameraPosTexcoord, ['%s' % adj_output], comment='New output with adjusted _WorldSpaceCameraPos')
    stereo_const, _ = insert_stereo_declarations(tree, args, w = WorldSpaceCameraPosMagicW)

    pos = tree.decl_end
    pos += tree.insert_instr(pos, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
    separation = t.x; convergence = t.y

    # This is similar to the _WorldSpaceCameraPos adjustment in the lighting
    # shaders, but instead of adjusting the coordinate in view-space and
    # transforming it to world-space (which would require matrices we don't
    # have handy), we adjust the coordinate in projection space, then transform
    # it to local-space using the inverse MVP matrix (inverted via Helix Mod),
    # then finally to world-space
    repl_cam_pos = tree._find_free_reg('r', VS3)
    clip_space_adj = tree._find_free_reg('r', VS3)
    local_space_adj = tree._find_free_reg('r', VS3)
    world_space_adj = clip_space_adj # Reuse the same register
    replace_regs = {_WorldSpaceCameraPos: repl_cam_pos}
    tree.do_replacements(replace_regs, False)

    pos += insert_vanity_comment(args, tree, pos, "Unity reflection/specular fix (object shader variant) inserted with")
    pos += tree.insert_instr(pos, NewInstruction('mov', [repl_cam_pos, _WorldSpaceCameraPos]))
    pos += tree.insert_instr(pos, NewInstruction('mov', [clip_space_adj, tree.stereo_const.x]))
    pos += tree.insert_instr(pos, NewInstruction('mul', [clip_space_adj.x, separation, -convergence]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.x, inv_mvp0, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.y, inv_mvp1, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.z, inv_mvp2, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.w, inv_mvp3, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.x, _Object2World0, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.y, _Object2World1, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.z, _Object2World2, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('add', [repl_cam_pos.xyz, repl_cam_pos, -world_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('mov', [adj_output, repl_cam_pos]))
    pos += tree.insert_instr(pos, NewInstruction('mov', [adj_output.w, stereo_const.w]))
    pos += tree.insert_instr(pos)

    if not hasattr(tree, 'ini'):
        tree.ini = []
    tree.ini.append(('GetMatrixFromReg1', str(unity_glstate_matrix_mvp.num),
        'Inverse MVP matrix for _WorldSpaceCameraPos adjustment:'))
    tree.ini.append(('InverseMatrix1', 'true', None))
    tree.ini.append(('UseMatrix1', 'true', None))
    tree.ini.append(('MatrixReg1', str(inv_mvp0.num), None))

    tree.autofixed = True

# FIXME: Refactor, or maybe just drop altogether since there is a more
# universal version now
def fix_unity_reflection_vs_variant_ps(tree, args):
    try:
        match = find_header(tree, unity_WorldSpaceCameraPos)
    except KeyError:
        debug_verbose(0, 'Shader does not use _WorldSpaceCameraPos')
        return
    _WorldSpaceCameraPos = Register('c' + match.group('constant'))

    try:
        d = find_declaration(tree, WorldSpaceCameraPosTexcoord)
    except:
        pass
    else:
        debug_verbose(0, 'Shader already uses %s' % WorldSpaceCameraPosTexcoord)
        raise NoFreeRegisters(WorldSpaceCameraPosTexcoord)

    t = tree._find_free_reg('r', PS3, desired=31)
    adj_input = tree._find_free_reg('v', PS3)
    tree.insert_decl()
    tree.insert_decl(WorldSpaceCameraPosTexcoord, ['%s' % adj_input], comment='New input with adjusted _WorldSpaceCameraPos')
    stereo_const, _ = insert_stereo_declarations(tree, args, w = WorldSpaceCameraPosMagicW)

    pos = tree.decl_end

    repl_cam_pos = tree._find_free_reg('r', PS3, desired=30)
    replace_regs = {_WorldSpaceCameraPos: repl_cam_pos}
    tree.do_replacements(replace_regs, False)

    pos += insert_vanity_comment(args, tree, pos, "Unity reflection/specular fix (object shader variant) inserted with")
    pos += tree.insert_instr(pos, NewInstruction('mov', [repl_cam_pos, adj_input]))
    pos += tree.insert_instr(pos, NewInstruction('add', [repl_cam_pos.w, repl_cam_pos.w, -stereo_const.w]))
    pos += tree.insert_instr(pos, NewInstruction('abs', [repl_cam_pos.w, repl_cam_pos.w]))
    pos += tree.insert_instr(pos, NewInstruction('if_gt', [repl_cam_pos.w, stereo_const.y]))
    pos += tree.insert_instr(pos, NewInstruction('mov', [repl_cam_pos, _WorldSpaceCameraPos]))
    pos += tree.insert_instr(pos, NewInstruction('endif', []))
    pos += tree.insert_instr(pos)

    tree.autofixed = True

# FIXME: Refactor, or maybe just drop altogether since there is a more
# universal version now
def fix_unity_reflection_ps_variant_vs(tree, args):
    try:
        match = find_header(tree, unity_Object2World)
    except KeyError:
        debug_verbose(0, 'Shader does not use _Object2World, or is missing headers (my other scripts can extract these)')
        return
    _Object2World0 = Register('c' + match.group('matrix'))
    _Object2World1 = Register('c%i' % (_Object2World0.num + 1))
    _Object2World2 = Register('c%i' % (_Object2World0.num + 2))

    try:
        match = find_header(tree, unity_glstate_matrix_mvp_pattern)
    except KeyError:
        debug_verbose(0, 'Shader does not use glstate_matrix_mvp')
        return
    unity_glstate_matrix_mvp = Register('c' + match.group('matrix'))

    try:
        d = find_declaration(tree, 'dcl_texcoord8')
    except:
        pass
    else:
        debug_verbose(0, 'Shader already uses %s' % 'texcoord8')
        raise NoFreeRegisters('texcoord8')
    try:
        d = find_declaration(tree, 'dcl_texcoord9')
    except:
        pass
    else:
        debug_verbose(0, 'Shader already uses %s' % 'texcoord9')
        raise NoFreeRegisters('texcoord9')
    try:
        d = find_declaration(tree, 'dcl_texcoord10')
    except:
        pass
    else:
        debug_verbose(0, 'Shader already uses %s' % 'texcoord10')
        raise NoFreeRegisters('texcoord10')

    out_Object2World0 = tree._find_free_reg('o', VS3)
    out_Object2World1 = tree._find_free_reg('o', VS3)
    out_Object2World2 = tree._find_free_reg('o', VS3)
    tree.insert_decl()
    tree.insert_decl('dcl_texcoord8', ['%s' % out_Object2World0], comment='New output with _Object2World[0]')
    tree.insert_decl('dcl_texcoord9', ['%s' % out_Object2World1], comment='New output with _Object2World[1]')
    tree.insert_decl('dcl_texcoord10', ['%s' % out_Object2World2], comment='New output with _Object2World[2]')
    tree.insert_decl()

    pos = tree.decl_end
    pos += tree.insert_instr(pos, NewInstruction('mov', [out_Object2World0, _Object2World0]))
    pos += tree.insert_instr(pos, NewInstruction('mov', [out_Object2World1, _Object2World1]))
    pos += tree.insert_instr(pos, NewInstruction('mov', [out_Object2World2, _Object2World2]))
    pos += tree.insert_instr(pos)

    if not hasattr(tree, 'ini'):
        tree.ini = []
    tree.ini.append(('GetMatrixFromReg1', str(unity_glstate_matrix_mvp.num),
        'Inverse MVP matrix for _WorldSpaceCameraPos adjustment (pixel shader variant):'))
    tree.ini.append(('InverseMatrix1', 'true', None))

    tree.autofixed = True

# FIXME: Refactor, or maybe just drop altogether since there is a more
# universal version now
def fix_unity_reflection_ps_variant_ps(tree, args):
    try:
        match = find_header(tree, unity_WorldSpaceCameraPos)
    except KeyError:
        debug_verbose(0, 'Shader does not use _WorldSpaceCameraPos')
        return
    _WorldSpaceCameraPos = Register('c' + match.group('constant'))

    inv_mvp0 = tree._find_free_reg('c', VS3, desired=180)
    # FIXME: Confirm these are free too:
    inv_mvp1 = Register('c%i' % (inv_mvp0.num + 1))
    inv_mvp2 = Register('c%i' % (inv_mvp0.num + 2))
    inv_mvp3 = Register('c%i' % (inv_mvp0.num + 3))

    try:
        d = find_declaration(tree, 'dcl_texcoord8')
    except:
        pass
    else:
        debug_verbose(0, 'Shader already uses %s' % 'texcoord8')
        raise NoFreeRegisters('texcoord8')
    try:
        d = find_declaration(tree, 'dcl_texcoord9')
    except:
        pass
    else:
        debug_verbose(0, 'Shader already uses %s' % 'texcoord9')
        raise NoFreeRegisters('texcoord9')
    try:
        d = find_declaration(tree, 'dcl_texcoord10')
    except:
        pass
    else:
        debug_verbose(0, 'Shader already uses %s' % 'texcoord10')
        raise NoFreeRegisters('texcoord10')

    t = tree._find_free_reg('r', PS3, desired=31)
    _Object2World0 = tree._find_free_reg('v', PS3)
    _Object2World1 = tree._find_free_reg('v', PS3)
    _Object2World2 = tree._find_free_reg('v', PS3)
    tree.insert_decl()
    tree.insert_decl('dcl_texcoord8', ['%s' % _Object2World0], comment='New input with _Object2World[0]')
    tree.insert_decl('dcl_texcoord9', ['%s' % _Object2World1], comment='New input with _Object2World[1]')
    tree.insert_decl('dcl_texcoord10', ['%s' % _Object2World2], comment='New input with _Object2World[2]')
    stereo_const, _ = insert_stereo_declarations(tree, args)

    pos = tree.decl_end
    pos += tree.insert_instr(pos, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
    separation = t.x; convergence = t.y

    # This is similar to the _WorldSpaceCameraPos adjustment in the lighting
    # shaders, but instead of adjusting the coordinate in view-space and
    # transforming it to world-space (which would require matrices we don't
    # have handy), we adjust the coordinate in projection space, then transform
    # it to local-space using the inverse MVP matrix (inverted via Helix Mod),
    # then finally to world-space
    repl_cam_pos = tree._find_free_reg('r', PS3)
    clip_space_adj = tree._find_free_reg('r', PS3)
    local_space_adj = tree._find_free_reg('r', PS3)
    world_space_adj = clip_space_adj # Reuse the same register
    replace_regs = {_WorldSpaceCameraPos: repl_cam_pos}
    tree.do_replacements(replace_regs, False)

    pos += insert_vanity_comment(args, tree, pos, "Unity reflection/specular fix (object *pixel* shader variant) inserted with")
    pos += tree.insert_instr(pos, NewInstruction('mov', [repl_cam_pos, _WorldSpaceCameraPos]))
    pos += tree.insert_instr(pos, NewInstruction('mov', [clip_space_adj, tree.stereo_const.x]))
    pos += tree.insert_instr(pos, NewInstruction('mul', [clip_space_adj.x, separation, -convergence]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.x, inv_mvp0, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.y, inv_mvp1, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.z, inv_mvp2, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.w, inv_mvp3, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.x, _Object2World0, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.y, _Object2World1, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.z, _Object2World2, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('add', [repl_cam_pos.xyz, repl_cam_pos, -world_space_adj]))
    pos += tree.insert_instr(pos)

    if not hasattr(tree, 'ini'):
        tree.ini = []
    tree.ini.append(('UseMatrix1', 'true',
        'Inverse MVP matrix for _WorldSpaceCameraPos adjustment (pixel shader variant):'))
    tree.ini.append(('MatrixReg1', str(inv_mvp0.num), None))

    tree.autofixed = True


# FIXME: Refactor, or maybe just drop altogether since there is a more
# universal version now
def fix_unity_reflection_vs_variant(tree, args):
    if isinstance(tree, VS3):
        return fix_unity_reflection_vs_variant_vs(tree, args)
    if isinstance(tree, PS3):
        return fix_unity_reflection_vs_variant_ps(tree, args)
    raise Exception('fix_unity_reflection_vs_variant called on something not shader model 3')

# FIXME: Refactor, or maybe just drop altogether since there is a more
# universal version now
def fix_unity_reflection_ps_variant(tree, args):
    if isinstance(tree, VS3):
        return fix_unity_reflection_ps_variant_vs(tree, args)
    if isinstance(tree, PS3):
        return fix_unity_reflection_ps_variant_ps(tree, args)
    raise Exception('fix_unity_reflection_ps_variant called on something not shader model 3')

# Universal variant of the above. Does not depend on passing information
# between shader stages (making it safer to apply in bulk), but does require
# the matrices to be copied with Helix Mod.
def fix_unity_reflection(tree, args):
    try:
        match = find_header(tree, unity_WorldSpaceCameraPos)
    except KeyError:
        debug_verbose(0, 'Shader does not use _WorldSpaceCameraPos')
        return
    _WorldSpaceCameraPos = Register('c' + match.group('constant'))

    inv_mvp0 = tree._find_free_reg('c', VS3, desired=180)
    # FIXME: Confirm these are free too:
    inv_mvp1 = Register('c%i' % (inv_mvp0.num + 1))
    inv_mvp2 = Register('c%i' % (inv_mvp0.num + 2))
    inv_mvp3 = Register('c%i' % (inv_mvp0.num + 3))
    _Object2World0 = tree._find_free_reg('c', None, desired=190)
    # FIXME: Confirm these are free too:
    _Object2World1 = Register('c%i' % (_Object2World0.num + 1))
    _Object2World2 = Register('c%i' % (_Object2World0.num + 2))

    t = tree._find_free_reg('r', None, desired=31)
    stereo_const, offset = insert_stereo_declarations(tree, args, w = 0.5)

    pos = tree.decl_end

    if tree.stereo_sampler is not None:
        pos += tree.insert_instr(pos, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
        separation = t.x; convergence = t.y

    clip_space_adj = tree._find_free_reg('r', None, desired=29)
    local_space_adj = tree._find_free_reg('r', None, desired=28)
    world_space_adj = clip_space_adj # Reuse the same register

    # Apply a stereo correction to the world space camera position - this
    # pushes environment reflections, specular highlights, etc to the correct
    # depth
    repl_cam_pos = tree._find_free_reg('r', None, desired=30)
    replace_regs = {_WorldSpaceCameraPos: repl_cam_pos}
    tree.do_replacements(replace_regs, False)
    pos += insert_vanity_comment(args, tree, pos, "Unity reflection/specular fix inserted with")
    pos += tree.insert_instr(pos, NewInstruction('mov', [repl_cam_pos, _WorldSpaceCameraPos]))
    pos += tree.insert_instr(pos, NewInstruction('mov', [clip_space_adj, tree.stereo_const.x]))

    if tree.stereo_sampler is not None:
        # Use Helix Mod stereo texture:
        pos += tree.insert_instr(pos, NewInstruction('mul', [clip_space_adj.x, separation, -convergence]))
    else:
        # Use undocumented register injected by 3D Vision driver:
        neg_sep_conv = tree.nv_stereo_reg.x; separation = tree.nv_stereo_reg.y
        pos += tree.insert_instr(pos, NewInstruction('mov', [clip_space_adj.x, neg_sep_conv]))

    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.x, inv_mvp0, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.y, inv_mvp1, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.z, inv_mvp2, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.w, inv_mvp3, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.x, _Object2World0, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.y, _Object2World1, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.z, _Object2World2, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('add', [repl_cam_pos.xyz, repl_cam_pos, -world_space_adj]))
    pos += tree.insert_instr(pos)

    if not hasattr(tree, 'ini'):
        tree.ini = []
    tree.ini.append(('UseMatrix', 'true',
        'Copy inversed MVP matrix and _Object2World matrix in for Unity reflection fix'))
    tree.ini.append(('MatrixReg', str(inv_mvp0.num), None))
    tree.ini.append(('UseMatrix1', 'true', None))
    tree.ini.append(('MatrixReg1', str(_Object2World0.num), None))

    # We might possibly use this shader as a source of the MVP and
    # _Object2World matrices, but only if it is rendering from the POV of the
    # camera. Blacklist shadow casters which are rendered from the POV of a
    # light.
    #
    # No longer blacklisting IGNOREPROJECTOR shaders - I was never sure what
    # that tag signified, but it's clear that they do/can have valid matrices,
    # and some scenes (e.g. falling Dreamer in Dreamfall chapter 1) only have
    # the matrices we want in these shaders.
    #
    # This blacklisting may not be necessary - I doubt that any shadow casters
    # will have used _WorldSpaceCameraPos and we won't have got this far.
    unity_glstate_matrix_mvp = _Object2World0 = None
    try:
        match = find_header(tree, unity_headers_attached)
    except KeyError:
        debug('Skipping possible matrix source - shader does not have Unity headers attached so unable to check if it is a SHADOWCASTER')
        tree.ini.append((None, None, 'Skipping possible matrix source - shader does not have Unity headers attached so unable to check if it is a SHADOWCASTER'))
    else:
        try:
            match = find_header(tree, unity_tag_shadow_caster)
        except KeyError:
            try:
                match = find_header(tree, unity_glstate_matrix_mvp_pattern)
                unity_glstate_matrix_mvp = Register('c' + match.group('matrix'))
                match = find_header(tree, unity_Object2World)
                _Object2World0 = Register('c' + match.group('matrix'))
                tree.ini.append(('GetMatrixFromReg', str(unity_glstate_matrix_mvp.num),
                    'Candidate to obtain MVP and _Object2World matrices:'))
                tree.ini.append(('InverseMatrix', 'true', None))
                tree.ini.append(('GetMatrixFromReg1', str(_Object2World0.num), None))
            except KeyError:
                pass
        else:
            tree.ini.append((None, None, 'Skipping possible matrix source - shader is a SHADOWCASTER'))

    tree.autofixed = True

def fix_unity_frustum_world(tree, args):
    try:
        match = find_header(tree, unity_FrustumCornersWS)
    except KeyError:
        debug_verbose(0, 'Shader does not use _FrustumCornersWS, or is missing headers (my other scripts can extract these)')
        return
    _FrustumCornersWS = [ Register('c' + match.group('matrix')) ]
    for i in range(1, 4):
        _FrustumCornersWS.append(Register('c%i' % (_FrustumCornersWS[0].num + i)))

    inv_mvp0 = tree._find_free_reg('c', None, desired=180)
    # FIXME: Confirm these are free too:
    inv_mvp1 = Register('c%i' % (inv_mvp0.num + 1))
    inv_mvp2 = Register('c%i' % (inv_mvp0.num + 2))
    inv_mvp3 = Register('c%i' % (inv_mvp0.num + 3))
    _Object2World0 = tree._find_free_reg('c', None, desired=190)
    # FIXME: Confirm these are free too:
    _Object2World1 = Register('c%i' % (_Object2World0.num + 1))
    _Object2World2 = Register('c%i' % (_Object2World0.num + 2))
    _ZBufferParams = tree._find_free_reg('c', None, desired=150)

    repl_frustum = []
    for i in range(4):
        repl_frustum.append( tree._find_free_reg('r', None))
        replace_regs = {_FrustumCornersWS[i]: repl_frustum[i]}
        tree.do_replacements(replace_regs, False)

    t = tree._find_free_reg('r', None, desired=31)
    stereo_const, offset = insert_stereo_declarations(tree, args, w = 0.5)

    pos = tree.decl_end

    if tree.stereo_sampler is not None:
        # Use Helix Mod stereo texture:
        pos += tree.insert_instr(pos, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
        separation = t.x; convergence = t.y

    clip_space_adj = tree._find_free_reg('r', None, desired=29)
    local_space_adj = tree._find_free_reg('r', None, desired=28)
    world_space_adj = clip_space_adj # Reuse the same register

    # Apply a stereo correction to the world space frustum corners - this
    # fixes the glow around the sun in The Forest (shaders called Sunshine
    # PostProcess Scatter)
    pos += insert_vanity_comment(args, tree, pos, "Unity _FrustumCornerWS fix inserted with")
    pos += tree.insert_instr(pos, NewInstruction('mov', [clip_space_adj, tree.stereo_const.x]))
    pos += tree.insert_instr(pos, NewInstruction('add', [clip_space_adj.x, _ZBufferParams.z, _ZBufferParams.w]), comment='Derive 1/far from _ZBufferParams')
    pos += tree.insert_instr(pos, NewInstruction('rcp', [clip_space_adj.x, clip_space_adj.x]))

    if tree.stereo_sampler is not None:
        # Use Helix Mod stereo texture:
        pos += tree.insert_instr(pos, NewInstruction('add', [clip_space_adj.x, clip_space_adj.x, -convergence]))
        pos += tree.insert_instr(pos, NewInstruction('mul', [clip_space_adj.x, clip_space_adj.x, separation]))
    else:
        # Use undocumented register injected by 3D Vision driver:
        neg_sep_conv = tree.nv_stereo_reg.x; separation = tree.nv_stereo_reg.y
        pos += tree.insert_instr(pos, NewInstruction('mad', [clip_space_adj.x, clip_space_adj.x, neg_sep_conv, separation]))

    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.x, inv_mvp0, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.y, inv_mvp1, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.z, inv_mvp2, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [local_space_adj.w, inv_mvp3, clip_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.x, _Object2World0, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.y, _Object2World1, local_space_adj]))
    pos += tree.insert_instr(pos, NewInstruction('dp4', [world_space_adj.z, _Object2World2, local_space_adj]))
    for i in range(4):
        pos += tree.insert_instr(pos, NewInstruction('add', [repl_frustum[i], _FrustumCornersWS[i], -world_space_adj]))
    pos += tree.insert_instr(pos)

    if not hasattr(tree, 'ini'):
        tree.ini = []
    tree.ini.append(('UseMatrix', 'true',
        'Copy inversed MVP matrix, _Object2World and _ZBufferParams in for Unity _FrustumCornerWS fix'))
    tree.ini.append(('MatrixReg', str(inv_mvp0.num), None))
    tree.ini.append(('UseMatrix1', 'true', None))
    tree.ini.append(('MatrixReg1', str(_Object2World0.num), None))
    tree.ini.append(('SetConst1ToReg', str(_ZBufferParams.num), None))

    tree.autofixed = True

def fix_unity_ssao(tree, args):
    if not isinstance(tree, PS3):
        raise Exception('Unity SSAO fix is only applicable to pixel shaders!')

    try:
        match = find_header(tree, unity_CameraDepthTexture)
    except KeyError:
        debug_verbose(0, 'Shader does not use _CameraDepthTexture')
        return
    _CameraDepthTexture = Register('s' + match.group('texture'))

    try:
        match = find_header(tree, unity_ZBufferParams)
    except KeyError:
        debug_verbose(0, 'Shader does not use _ZBufferParams, or is missing headers (my other scripts can extract these)')
        return
    _ZBufferParams = Register('c' + match.group('constant'))

    debug_verbose(0, '_CameraDepthTexture in %s, _ZBufferParams in %s' % (_CameraDepthTexture, _ZBufferParams))

    # Find _ZBufferParams usage to find where depth is sampled (could use
    # _CameraDepthTexture, but that takes an extra step)
    results = scan_shader(tree, _ZBufferParams, write=False)
    if len(results) == 0:
        debug_verbose(0, '_ZBufferParams read 0 times')
        return

    # Insert the stereo declarations and sampler. FIXME: We don't know for sure
    # yet that we will be modifying the shader - we should do this once we have
    # confirmed that we will
    t = tree._find_free_reg('r', PS3, desired=31)
    tmp = tree._find_free_reg('r', PS3, desired=30)
    pos = tree.decl_end
    stereo_const, offset = insert_stereo_declarations(tree, args, w = 0.5)

    if tree.stereo_sampler is not None:
        # Use Helix Mod stereo texture:
        offset += tree.insert_instr(pos + offset, NewInstruction('texldl', [t, stereo_const.z, tree.stereo_sampler]))
        separation = t.x; convergence = t.y

    offset += tree.insert_instr(pos + offset)

    vanity_inserted = False
    lines_done = set()
    for (line, linepos, instr) in results:
        if line in lines_done:
            continue
        lines_done.add(line)

        line += offset
        reg = instr.args[0]

        # We're expecting a reciprocal calculation as part of the Z buffer -> world
        # Z scaling:
        results = scan_shader(tree, reg.reg, components=reg.swizzle, opcode='rcp',
                write=False, start=line+1, stop=True)
        if not results:
            debug_verbose(0, 'Could not find expected rcp instruction')
            continue
        (line, linepos, instr) = results[0]
        reg = instr.args[0]

        # Find where the reciprocal is next used:
        results = scan_shader(tree, reg.reg, components=reg.swizzle, write=False,
                start=line+1, stop=True, opcode=('mad', 'mul'))
        if not results:
            debug_verbose(0, 'Could not find expected instruction')
            continue
        (line, linepos, instr) = results[0]
        reg = instr.args[0]

        if len(reg.swizzle) != 3:
            debug_verbose(0, 'Instruction has wrong dimensional swizzle - skipping')
            continue

        start_pos = pos = next_line_pos(tree, line)

        debug('Inserting stereo uncorrection at line %i: %s' % (line, instr))

        if not vanity_inserted:
            pos += insert_vanity_comment(args, tree, pos, "Unity SSAO fix inserted with")
            vanity_inserted = True
        else:
            pos += tree.insert_instr(pos);

        if instr.opcode == 'mul':
            x_reg = Register('%s.%s' % (reg.reg, reg.swizzle[0]))
            depth = Register('%s.%s' % (reg.reg, reg.swizzle[2]))

            if tree.stereo_sampler is not None:
                # Use Helix Mod stereo texture:
                pos += tree.insert_instr(pos, NewInstruction('add', [t.w, depth, -convergence]))
                pos += tree.insert_instr(pos, NewInstruction('mad', [x_reg, t.w, separation, x_reg]))
            else:
                # Use undocumented register injected by 3D Vision driver:
                neg_sep_conv = tree.nv_stereo_reg.x; separation = tree.nv_stereo_reg.y
                pos += tree.insert_instr(pos, NewInstruction('mad', [x_reg, depth, neg_sep_conv, x_reg]))
                pos += tree.insert_instr(pos, NewInstruction('add', [x_reg, x_reg, separation]))

        else:
            tree[line].insert(0, CPPStyleComment('// '))
            tree[line].append(WhiteSpace(' '))
            tree[line].append(CPPStyleComment('// Instruction split into mul and add with shadertool.py'))
            tmp_reg = Register('%s.%s' % (tmp.reg, reg.swizzle))
            pos += tree.insert_instr(pos, NewInstruction('mul', [tmp_reg, instr.args[1], instr.args[2]]))
            x_reg = Register('%s.%s' % (tmp.reg, reg.swizzle[0]))
            depth = Register('%s.%s' % (tmp.reg, reg.swizzle[2]))

            if tree.stereo_sampler is not None:
                # Use Helix Mod stereo texture:
                pos += tree.insert_instr(pos, NewInstruction('add', [t.w, depth, -convergence]))
                pos += tree.insert_instr(pos, NewInstruction('mad', [x_reg, t.w, separation, x_reg]))
            else:
                # Use undocumented register injected by 3D Vision driver:
                neg_sep_conv = tree.nv_stereo_reg.x; separation = tree.nv_stereo_reg.y
                pos += tree.insert_instr(pos, NewInstruction('mad', [x_reg, depth, neg_sep_conv, x_reg]))
                pos += tree.insert_instr(pos, NewInstruction('add', [x_reg, x_reg, separation]))

            pos += tree.insert_instr(pos, NewInstruction('add', [instr.args[0], tmp, instr.args[3]]))

        pos += tree.insert_instr(pos);

        offset += pos - start_pos

    tree.autofixed = True

def add_unity_autofog_VS3(tree, reason):
    try:
        d = find_declaration(tree, 'dcl_fog')
        debug_verbose(0, 'Shader already has a fog output: %s' % d)
        return
    except:
        pass

    if 'o' in tree.reg_types and 'o9' in tree.reg_types['o']:
        debug('Shader already uses output o9')
        return

    pos_out = find_declaration(tree, 'dcl_position', 'o')

    results = scan_shader(tree, pos_out, write=True)
    if len(results) != 1:
        debug_verbose(0, 'Output position written from %i instructions (only exactly 1 write currently supported)' % len(results))
        return
    (output_line, output_linepos, output_instr) = results[0]
    if output_instr.opcode != 'mov':
        debug('Output not using mov instruction: %s' % output_instr)
        return
    temp_reg = output_instr.args[1]
    if not temp_reg.startswith('r'):
        debug('Output not moved from a temporary register: %s' % output_instr)
        return

    tree.fog_type = 'FOG'
    fog_output = NewInstruction('mov', [Register('o9'), temp_reg.z])
    tree.insert_instr(next_line_pos(tree, output_line), fog_output, 'Inserted by shadertool.py %s' % reason)
    debug_verbose(-1, "Line %i: %s" % (pos_to_line(tree, output_line+2), tree[output_line+2]))
    decl = NewInstruction('dcl_fog', [Register('o9')])
    # Inserting this in a specific spot to match Unity rather than using
    # insert_decl(), so manually increment decl_end as well:
    tree.decl_end += tree.insert_instr(next_line_pos(tree, tree.shader_start), decl, 'Inserted by shadertool.py %s' % reason)

def add_unity_autofog_PS3(tree, mad_fog, reason):
    try:
        d = find_declaration(tree, 'dcl_fog')
        debug_verbose(0, 'Shader already has a fog input: %s' % d)
        return
    except:
        pass

    if 'v' in tree.reg_types and 'v9' in tree.reg_types['v']:
        debug('Shader already uses input v9')
        return

    if 'r' in tree.reg_types and 'r30' in tree.reg_types['r']:
        debug('Shader already uses temporary register r30')
        return

    if 'r' in tree.reg_types and 'r31' in tree.reg_types['r']:
        debug('Shader already uses temporary register r31')
        return

    fog_c1 = tree._find_free_reg('c', None)
    fog_c2 = tree._find_free_reg('c', None)

    if fog_c2.num != fog_c1.num + 1:
        debug('Discontiguous free constants, not sure how Unity handles this edge case so aborting')
        return

    decl = NewInstruction('dcl_fog', ['v9.x'])
    tree.insert_instr(next_line_pos(tree, tree.shader_start), decl, 'Inserted by shadertool.py %s' % reason)

    replace_regs = {'oC0': Register('r30')}
    tree.do_replacements(replace_regs, False)

    pos = len(tree) + 1

    def add_instr(opcode, args):
        return tree.insert_instr(pos, NewInstruction(opcode, args))

    pos += tree.insert_instr(pos, None, 'Inserted by shadertool.py %s:' % reason)

    if mad_fog:
        tree.fog_type = 'MAD_FOG'
        pos += add_instr('mad_sat', ['r31.x', fog_c2.z, 'v9.x', fog_c2.w])
    else:
        tree.fog_type = 'EXP_FOG'
        pos += add_instr('mul', ['r31.x', fog_c2.x, 'v9.x'])
        pos += add_instr('mul', ['r31.x', 'r31.x', 'r31.x'])
        pos += add_instr('exp_sat', ['r31.x', '-r31.x'])

    debug_verbose(-1, "Inserting pixel shader fog instructions (%s)" % tree.fog_type)

    pos += add_instr('lrp', ['r30.xyz', 'r31.x', 'r30', fog_c1])
    pos += add_instr('mov', ['oC0', 'r30'])

def add_unity_autofog(tree, reason = 'to match Unity autofog'):
    '''
    Adds instructions to a shader to match those Unity automatically adds for
    fog. Used by extract_unity_shaders.py to construct fog variants of shaders.
    Returns a tuple of trees with each type of fog added.
    '''
    if not hasattr(tree, 'reg_types'):
        tree.analyse_regs()
    if isinstance(tree, VS3):
        add_unity_autofog_VS3(tree, reason)
        return (tree,)
    if isinstance(tree, PS3):
        tree1 = copy.deepcopy(tree)
        tree2 = copy.deepcopy(tree)
        add_unity_autofog_PS3(tree1, True, reason)
        add_unity_autofog_PS3(tree2, False, reason)
        return (tree1, tree2)
    return (tree,)

def _disable_output(tree, reg, args, stereo_const, tmp_reg):
    pos_reg = tree._find_free_reg('r', VS3)

    if reg.startswith('dcl_texcoord'):
        reg = find_declaration(tree, reg, 'o').reg
    elif reg.startswith('texcoord'):
        reg = find_declaration(tree, 'dcl_%s' % reg, 'o').reg

    disabled = stereo_const.xxxx

    append_vanity_comment(args, tree, 'Texcoord disabled by')
    # TODO: Add support for --use-nv-stereo-reg-vs
    tree.add_inst('texldl', [tmp_reg, stereo_const.z, tree.stereo_sampler])
    separation = tmp_reg.x
    tree.add_inst('if_ne', [separation, -separation]) # Only disable in 3D
    if args.condition:
        tree.add_inst('mov', [tmp_reg.w, args.condition])
        tree.add_inst('if_eq', [tmp_reg.w, stereo_const.x])
    tree.add_inst('mov', [reg, disabled])
    if args.condition:
        tree.add_inst('endif', [])
    tree.add_inst('endif', [])

def disable_output(tree, args):
    if not isinstance(tree, VS3):
        raise Exception('Texcoord adjustment must be done on a vertex shader (currently)')

    stereo_const, _ = insert_stereo_declarations(tree, args)

    tmp_reg = tree._find_free_reg('r', VS3, desired=31)

    success = False

    for reg in args.disable_output:
        try:
            _disable_output(tree, reg, args, stereo_const, tmp_reg)
            success = True
        except Exception as e:
            if args.ignore_other_errors:
                collected_errors.append((tree.filename, e))
                import traceback, time
                traceback.print_exc()
                last_exc = e
                continue
            raise

    if not success and last_exc is not None:
        # We have already reported this exception, but since none of the inputs
        # were adjusted we don't want this shader to be installed. Raise a
        # special exception to skip it without double reporting the error.
        raise ExceptionDontReport()

def disable_shader(tree, args):
    if isinstance(tree, VS3):
        reg = find_declaration(tree, 'dcl_position', 'o')
        if not reg.swizzle:
            reg = '%s.xyzw' % reg.reg
    elif isinstance(tree, PS3):
        reg = 'oC0.xyzw'
    else:
        raise Exception("Shader must be a vs_3_0 or a ps_3_0, but it's a %s" % shader.__class__.__name__)

    # FUTURE: Maybe search for an existing 0 or 1...
    stereo_const, _ = insert_stereo_declarations(tree, args)
    tmp_reg = tree._find_free_reg('r', VS3, desired=31)

    append_vanity_comment(args, tree, 'Shader disabled by')
    if args.disable == '0':
        disabled = stereo_const.xxxx
    if args.disable == '1':
        disabled = stereo_const.yyyy

    # TODO: Add support for --use-nv-stereo-reg-vs
    tree.add_inst('texldl', [tmp_reg, stereo_const.z, tree.stereo_sampler])
    separation = tmp_reg.x
    tree.add_inst('if_ne', [separation, -separation]) # Only disable in 3D
    if args.condition:
        tree.add_inst('mov', [tmp_reg.w, args.condition])
        tree.add_inst('if_eq', [tmp_reg.w, stereo_const.x])
    tree.add_inst('mov', [reg, disabled])
    if args.condition:
        tree.add_inst('endif', [])
    tree.add_inst('endif', [])

def lookup_header_json(tree, index, file):
    if len(tree) and len(tree[0]) and isinstance(tree[0][0], CPPStyleComment) \
            and tree[0][0].startswith('// CRC32'):
                debug_verbose(0, '%s appears to already contain headers' % file)
                return tree

    crc = shaderutil.get_filename_crc(file)
    try:
        headers = index[crc]
    except:
        debug('%s not found in header index' % crc)
        return tree
    headers = [ (CPPStyleComment(x), NewLine('\n')) for x in headers.split('\n') ]
    headers = type(tree)(itertools.chain(*headers), None)
    headers.append(NewLine('\n'))
    headers.shader_start = len(headers) + tree.shader_start
    headers.decl_end = len(headers) + tree.decl_end
    headers.extend(tree)
    return headers

def update_ini(tree):
    '''
    Right now this just updates our internal data structures to note any
    changes we need to make to the ini file and we print these out before
    exiting. TODO: Actually update the ini file for real (still should notify
    the user).
    '''
    if not hasattr(tree, 'ini'):
        return

    if isinstance(tree, VertexShader):
        acronym = 'VS'
    elif isinstance(tree, PixelShader):
        acronym = 'PS'
    else:
        raise AssertionError()

    crc = shaderutil.get_filename_crc(tree.filename)
    section = '%s%s' % (acronym, crc)
    dx9settings_ini.setdefault(section, [])
    for (k, v, comment) in tree.ini:
        if comment is not None:
            dx9settings_ini[section].append('; %s' % comment)
        if k is not None:
            dx9settings_ini[section].append((k, v))

def do_ini_updates():
    if not dx9settings_ini:
        return

    # TODO: Merge these into the ini file directly. Still print a message
    # for the user so they know what we've done.
    debug()
    debug()
    debug('!' * 79)
    debug('!' * 12 + ' Please add the following lines to the DX9Settings.ini ' + '!' * 12)
    debug('!' * 79)
    debug()
    for section in sorted(dx9settings_ini):
        write_ini('[%s]' % section)
        for line in dx9settings_ini[section]:
            if isinstance(line, tuple):
                write_ini('%s = %s' % line)
            else:
                write_ini(line)
        write_ini()

def show_collected_errors():
    if not collected_errors:
        return
    debug()
    debug()
    debug('!' * 79)
    debug('!' * 11 + ' The following shaders had errors and were not processed ' + '!' * 11)
    debug('!' * 79)
    debug()
    for (filename, exception) in collected_errors:
        debug('%s: %s: %s' % (filename, exception.__class__.__name__, str(exception)))

def parse_args():
    global verbosity

    parser = argparse.ArgumentParser(description = 'nVidia 3D Vision Shaderhacker Tool')
    parser.add_argument('files', nargs='+',
            help='List of shader assembly files to process')
    parser.add_argument('--install', '-i', action='store_true',
            help='Install shaders in ShaderOverride directory')
    parser.add_argument('--install-to', '-I',
            help='Install shaders under ShaderOverride in a custom directory')
    parser.add_argument('--to-git', '--git', action='store_true',
            help='Copy the file to the location of this script, guessing the name of the game. Implies --no-convert and --force')
    parser.add_argument('--force', '-f', action='store_true',
            help='Forcefully overwrite shaders when installing')
    parser.add_argument('--output', '-o', type=argparse.FileType('w'),
            help='Save the shader to a file')
    parser.add_argument('--in-place', action='store_true',
            help='Overwrite the file in-place')

    parser.add_argument('--stereo-sampler-vs',
            help='Specify the sampler to read the stereo parameters from in vertex shaders')
    parser.add_argument('--stereo-sampler-ps',
            help='Specify the sampler to read the stereo parameters from in pixel shaders')
    parser.add_argument('--use-nv-stereo-reg-vs', nargs='?', default=None, const='c255',
            help='Use the undocumented stereo register injected by the driver instead of the stereo texture injected by Helix Mod. This is only supported by some options in this tool, and is only available in vertex shaders that the driver adjusted, so it is not recommended for use.')

    parser.add_argument('--show-regs', '-r', action='store_true',
            help='Show the registers used in the shader')
    parser.add_argument('--find-free-consts', '--consts', '-c', action='store_true',
            help='Search for unused constants')
    parser.add_argument('--disable', choices=['0', '1'], nargs='?', default=None, const='0',
            help="Disable a shader, by setting it's output to 0 or 1")
    parser.add_argument('--disable-output', '--disable-texcoord', action='append',
            help="Disable a given texcoord in the shader")
    parser.add_argument('--adjust', '--adjust-output', '--adjust-texcoord', action='append',
            help="Apply the stereo formula to an output (texcoord or position)")
    parser.add_argument('--adjust-input', action='append',
            help="Apply the stereo formula to a shader input")
    parser.add_argument('--adjust-multiply', '--adjust-multiplier', '--multiply', type=float,
            help="Multiplier for the stereo adjustment. If you notice the broken effect switches eyes try 0.5")
    parser.add_argument('--unadjust', action='append', nargs='?', default=None, const='position',
            help="Unadjust the output. Equivalent to --adjust=<output> --adjust-multiply=-1")
    parser.add_argument('--condition',
            help="Make adjustments conditional on the given register passed in from DX9Settings.ini")
    parser.add_argument('--auto-fix-vertex-halo', action='store_true',
            help="Attempt to automatically fix a vertex shader for common halo type issues")
    parser.add_argument('--disable-redundant-unreal-correction', action='store_true',
            help="Disable the stereo correction in Unreal Engine pixel shaders that also use the vPos semantic")
    parser.add_argument('--auto-fix-unreal-light-shafts', action='store_true',
            help="Attempt to automatically fix light shafts found in Unreal games")
    parser.add_argument('--auto-fix-unreal-dne-reflection', action='store_true',
            help="Attempt to automatically fix reflective floor surfaces found in Unreal games")
    parser.add_argument('--auto-fix-unreal-shadows', action='store_true',
            help="Attempt to automatically fix shadows in Unreal games")
    parser.add_argument('--auto-fix-unreal-lights', action='store_true',
            help="Attempt to automatically fix lights in Unreal games")
    parser.add_argument('--fix-unreal-halo-vpm', action='store_true',
            help="Fix halos caused by the view projection matrix used in the pixel shader of certain UE3 games")
    parser.add_argument('--fix-unreal-halo-sct', action='store_true',
            help="Fix halos on effects that read the SceneColorTexture in certain UE3 games")
    parser.add_argument('--fix-unity-lighting-ps', action='store_true',
            help="Apply a correction to Unity lighting pixel shaders. NOTE: This is only one part of the Unity lighting fix! You should use my template instead!")
    parser.add_argument('--fix-unity-lighting-ps-world', action='store_true',
            help="Apply a correction to Unity lighting pixel shaders using the world-space variation (goal is to reduce the number of matrices that need to be copied if world-space coordinates need adjusting elsewhere. As above, this also needs a fix in the vertex shader).")
    parser.add_argument('--fix-unity-reflection', action='store_true',
            help="Correct the Unity camera position to fix certain cases of specular highlights, reflections and some fake transparent windows. Requires a valid MVP obtained and inverted with GetMatrixFromReg and a valid _Object2World matrix obtained with GetMatrix1FromReg")
    parser.add_argument('--fix-unity-reflection-vs', action='store_true',
            help="Variant the above that calculates the adjustment in the vertex shader and passes it through to the pixel shader (does not require matrices copied from elsewhere, but does require a spare output)")
    parser.add_argument('--fix-unity-reflection-ps', action='store_true',
            help="Variant of the above that applies the fix in the pixel shader using the _Object2World matrix passed from the vertex shader - use when neither above options are suitable and the vertex shader has three spare outputs")
    parser.add_argument('--fix-unity-frustum-world', '--fix-unity-frustrum-world', action='store_true',
            help="Applies a world-space correction to _FrustumCornersWS. Requires a valid MVP obtained and inverted with GetMatrixFromReg, a valid _Object2World matrix obtained with GetMatrix1FromReg, and _ZBufferParams obtained with GetConst1FromReg")
    parser.add_argument('--fix-unity-ssao', action='store_true',
            help="(WORK IN PROGRESS) Attempts to autofix various 3rd party SSAO shaders found in certain Unity games")
    parser.add_argument('--adjust-unity-ceto-reflections', action='store_true',
            help="Attempt to automatically fix reflections in Unity Ceto Ocean shader")
    parser.add_argument('--only-autofixed', action='store_true',
            help="Installation type operations only act on shaders that were successfully autofixed with --auto-fix-vertex-halo")

    parser.add_argument('--add-unity-autofog', action='store_true',
            help="Add instructions to the shader to support fog, like Unity does (used by extract_unity_shaders.py)")
    parser.add_argument('--add-fog-on-sm3-update', action='store_true',
            help="Add fog instructions to any vertex shader being upgraded from vs_2_0 to vs_3_0 - use if fog disappears on a shader after running through this tool")

    parser.add_argument('--no-convert', '--noconv', action='store_false', dest='auto_convert',
            help="Do not automatically convert shaders to shader model 3")
    parser.add_argument('--lookup-header-json', type=argparse.FileType('r'), # XXX: When python 3.4 comes to cygwin, add encoding='utf-8'
            help="Look up headers in a JSON index, such as those created with extract_unity_shaders.py and prepend them.\n" +
            "Implies --no-convert --in-place if no other installation options were provided")
    parser.add_argument('--original', action='store_true',
            help="Look for the original shader from Dumps/AllDumps and work as though it had been specified instead")
    parser.add_argument('--restore-original', '--restore-original-shader', action='store_true',
            help="Look for an original copy of the shader in the Dumps/AllShaders directory and copies it over the top of this one\n" +
            "Game must have been run with DumpAll=true in the past. Implies --in-place --no-convert")
    parser.add_argument('--adjust-ui-depth', '--ui',
            help='Adjust the output depth of this shader to a percentage of separation passed in from DX9Settings.ini')

    parser.add_argument('--debug-tokeniser', action='store_true',
            help='Dumps the shader broken up into tokens')
    parser.add_argument('--debug-syntax-tree', action='store_true',
            help='Dumps the syntax tree')
    parser.add_argument('--ignore-parse-errors', action='store_true',
            help='Continue with the next file in the event of a parse error')
    parser.add_argument('--ignore-other-errors', action='store_true',
            help='Continue with the next file in the event of some other error while applying a fix')
    parser.add_argument('--ignore-register-errors', action='store_true',
            help='Continue with the next file in the event that a fix cannot be applied due to running out of registers')

    parser.add_argument('--verbose', '-v', action='count', default=0,
            help='Level of verbosity')
    parser.add_argument('--quiet', '-q', action='count', default=0,
            help='Suppress usual informational messages. Specify multiple times to suppress more messages.')
    args = parser.parse_args()

    if not args.output and not args.in_place and not args.install and not \
            args.install_to and not args.to_git and not args.find_free_consts \
            and not args.show_regs and not args.debug_tokeniser and not \
            args.debug_syntax_tree and not args.restore_original and not \
            args.lookup_header_json:
        parser.error("did not specify anything to do (e.g. --install, --install-to, --in-place, --output, --show-regs, etc)");

    if args.to_git:
        if not args.output and not args.install and not args.install_to and not args.to_git:
            args.auto_convert = False

    if args.lookup_header_json:
        args.lookup_header_json = json.load(args.lookup_header_json)
        if not args.output and not args.install and not args.install_to and not args.to_git:
            args.in_place = True
            args.auto_convert = False

    args.precheck_installed = False
    if (args.install or args.install_to) and not args.force and not args.output and not args.to_git:
        args.precheck_installed = True

    if args.restore_original:
        args.auto_convert = False

    if args.add_unity_autofog:
        args.auto_convert = False

    verbosity = args.verbose - args.quiet

    return args

def args_require_reg_analysis(args):
        return args.show_regs or \
                args.find_free_consts or \
                args.adjust_ui_depth or \
                args.disable or \
                args.disable_output or \
                args.adjust or \
                args.adjust_input or \
                args.unadjust or \
                args.auto_fix_vertex_halo or \
                args.add_unity_autofog or \
                args.disable_redundant_unreal_correction or \
                args.auto_fix_unreal_light_shafts or \
                args.auto_fix_unreal_dne_reflection or \
                args.adjust_unity_ceto_reflections or \
                args.auto_fix_unreal_shadows or \
                args.auto_fix_unreal_lights or \
                args.fix_unreal_halo_vpm or \
                args.fix_unreal_halo_sct or \
                args.fix_unity_lighting_ps or \
                args.fix_unity_lighting_ps_world or \
                args.fix_unity_reflection or \
                args.fix_unity_reflection_vs or \
                args.fix_unity_reflection_ps or \
                args.fix_unity_frustum_world or \
                args.fix_unity_ssao

        # Also needs register analysis, but earlier than this test:
        # args.add_fog_on_sm3_update

def expand_wildcards(args):
    # Windows command prompt passes us a literal *, so expand any that we were passed:
    f = []
    for file in args.files:
        if '*' in file:
            f.extend(glob.glob(file))
        else:
            f.append(file)
    args.files = f

processed = set()

def main():
    args = parse_args()

    if args.find_free_consts:
        free_ps_consts = RegSet([ Register('c%d' % c) for c in range(PS3.max_regs['c']) ])
        free_vs_consts = RegSet([ Register('c%d' % c) for c in range(VS3.max_regs['c']) ])
        local_ps_consts = free_ps_consts.copy()
        local_vs_consts = free_vs_consts.copy()
        address_reg_vs = RegSet()
        address_reg_ps = RegSet()
        checked_ps = checked_vs = False

    expand_wildcards(args)

    for file in args.files:
        try:
            crc = shaderutil.get_filename_crc(file)
        except shaderutil.NoCRCError:
            pass
        else:
            if crc in processed:
                debug_verbose(1, 'Skipping %s - CRC already processed' % file)
                continue
            processed.add(crc)

        if args.precheck_installed and check_shader_installed(file, args):
            debug_verbose(0, 'Skipping %s - already installed and you did not specify --force' % file)
            continue

        if args.restore_original:
            debug('Restoring %s...' % file)
            restore_original_shader(file)
            continue

        real_file = file
        if args.original:
            file = find_original_shader(file)

        debug_verbose(-2, 'parsing %s...' % file)
        try:
            if file == '-':
                tree = parse_shader(sys.stdin.read(), args)
            else:
                tree = parse_shader(open(file, 'r', newline=None).read(), args)
        except Exception as e:
            if args.ignore_parse_errors:
                collected_errors.append((file, e))
                import traceback, time
                traceback.print_exc()
                continue
            show_collected_errors()
            do_ini_updates()
            raise
        if args.auto_convert and hasattr(tree, 'to_shader_model_3'):
            debug_verbose(0, 'Converting to Shader Model 3...')
            tree.to_shader_model_3(args)
        if args.debug_syntax_tree:
            debug(repr(tree), end='')

        if args.lookup_header_json:
            tree = lookup_header_json(tree, args.lookup_header_json, file)

        if args_require_reg_analysis(args):
            tree.analyse_regs(args.show_regs)
            if args.find_free_consts:
                if isinstance(tree, VS3):
                    checked_vs = True
                    local_vs_consts = local_vs_consts.difference(tree.global_consts)
                    free_vs_consts = free_vs_consts.difference(tree.consts)
                    address_reg_vs.update(tree.addressed_regs)
                elif isinstance(tree, PS3):
                    checked_ps = True
                    local_ps_consts = local_ps_consts.difference(tree.global_consts)
                    free_ps_consts = free_ps_consts.difference(tree.consts)
                    address_reg_ps.update(tree.addressed_regs)
                else:
                    raise Exception("Shader must be a vs_3_0 or a ps_3_0, but it's a %s" % shader.__class__.__name__)

        tree.filename = file

        try:
            if args.add_unity_autofog:
                # FIXME: Output both types on pixel shader fog or make selectable
                tree = add_unity_autofog(tree)[0]
            if args.disable:
                disable_shader(tree, args)
            tree.autofixed = False
            if args.auto_fix_vertex_halo:
                auto_fix_vertex_halo(tree, args)
            if args.disable_redundant_unreal_correction:
                disable_unreal_correction(tree, args, True)
            if args.auto_fix_unreal_light_shafts:
                auto_fix_unreal_light_shafts(tree, args)
            if args.auto_fix_unreal_dne_reflection:
                auto_fix_unreal_dne_reflection(tree, args)
            if args.adjust_unity_ceto_reflections:
                adjust_unity_ceto_reflections(tree, args)
            if args.auto_fix_unreal_shadows:
                auto_fix_unreal_shadows(tree, args)
            if args.auto_fix_unreal_lights:
                auto_fix_unreal_lights(tree, args)
            if args.fix_unreal_halo_vpm:
                fix_unreal_halo_vpm(tree, args)
            if args.fix_unreal_halo_sct:
                fix_unreal_halo_sct(tree, args)
            if args.fix_unity_lighting_ps:
                fix_unity_lighting_ps(tree, args)
            if args.fix_unity_lighting_ps_world:
                fix_unity_lighting_ps_world(tree, args)
            if args.fix_unity_reflection:
                fix_unity_reflection(tree, args)
            if args.fix_unity_reflection_vs:
                fix_unity_reflection_vs(tree, args)
            if args.fix_unity_reflection_ps:
                fix_unity_reflection_ps_variant(tree, args)
            if args.fix_unity_frustum_world:
                fix_unity_frustum_world(tree, args)
            if args.fix_unity_ssao:
                fix_unity_ssao(tree, args)
            if args.adjust_ui_depth:
                adjust_ui_depth(tree, args)
            if args.disable_output:
                disable_output(tree, args)
            if args.adjust:
                adjust_output(tree, args)
            if args.adjust_input:
                adjust_input(tree, args)
            if args.unadjust:
                a = copy.copy(args)
                a.adjust = args.unadjust
                a.adjust_multiply = -1
                adjust_output(tree, a)
        except (NoFreeRegisters, StereoSamplerAlreadyInUse) as e:
            if args.ignore_register_errors:
                collected_errors.append((file, e))
                import traceback, time
                traceback.print_exc()
                continue
            raise
        except ExceptionDontReport as e:
            # Exception has already been reported, we are just here to skip
            # installing the shader
            continue
        except Exception as e:
            if args.ignore_other_errors:
                collected_errors.append((file, e))
                import traceback, time
                traceback.print_exc()
                continue
            raise

        if not args.only_autofixed or tree.autofixed:
            if args.output:
                print(tree, end='', file=args.output)
                update_ini(tree)
            if args.in_place:
                tmp = '%s.new' % real_file
                print(tree, end='', file=open(tmp, 'w'))
                os.rename(tmp, real_file)
                update_ini(tree)
            if args.install:
                if install_shader(tree, file, args):
                    update_ini(tree)
            if args.install_to:
                if install_shader_to(tree, file, args, os.path.expanduser(args.install_to), True):
                    update_ini(tree)
            if args.to_git:
                a = copy.copy(args)
                a.force = True
                if install_shader_to_git(tree, file, a):
                    update_ini(tree)

    if args.find_free_consts:
        if checked_vs:
            local_vs_consts = local_vs_consts.difference(free_vs_consts)
            debug('\nFree Vertex Shader Constants:')
            debug(', '.join(sorted(free_vs_consts)))
            debug('\nLocal only Vertex Shader Constants:')
            debug(', '.join(sorted(local_vs_consts)))
            if address_reg_vs:
                debug('\nCAUTION: Address reg was applied offset from these consts:')
                debug(', '.join(sorted(address_reg_vs)))
        if checked_ps:
            local_ps_consts = local_ps_consts.difference(free_ps_consts)
            debug('\nFree Pixel Shader Constants:')
            debug(', '.join(sorted(free_ps_consts)))
            debug('\nLocal only Pixel Shader Constants:')
            debug(', '.join(sorted(local_ps_consts)))
            if address_reg_ps:
                debug('\nCAUTION: Address reg was applied offset from these consts:')
                debug(', '.join(sorted(address_reg_ps)))

    show_collected_errors()
    do_ini_updates()

if __name__ == '__main__':
    sys.exit(main())

# vi: et ts=4:sw=4
