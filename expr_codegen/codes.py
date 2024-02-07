import ast
import re

from expr_codegen.expr import register_symbols, dict_to_exprs


class SympyTransformer(ast.NodeTransformer):
    """将ast转换成Sympy要求的格式"""

    # 旧记录
    funcs_old = set()
    args_old = set()
    targets_old = set()
    # 旧记录
    funcs_new = set()
    args_new = set()
    targets_new = set()

    # 映射
    funcs_map = {}
    args_map = {}
    targets_map = {}

    def config_map(self, funcs_map, args_map, targets_map):
        self.funcs_map = funcs_map
        self.args_map = args_map
        self.targets_map = targets_map

    def visit_Call(self, node):
        # 提取函数名
        self.funcs_old.add(node.func.id)
        node.func.id = self.funcs_map.get(node.func.id, node.func.id)
        self.funcs_new.add(node.func.id)
        # 提取参数名
        for arg in node.args:
            if isinstance(arg, ast.Name):
                self.args_old.add(arg.id)
                arg.id = self.args_map.get(arg.id, arg.id)
                self.args_new.add(arg.id)

        self.generic_visit(node)
        return node

    def visit_Assign(self, node):
        # 提取输出变量名
        for target in node.targets:
            if isinstance(target, ast.Tuple):
                for t in target.elts:
                    self.targets_old.add(t.id)
                    t.id = self.targets_map.get(t.id, t.id)
                    self.targets_new.add(t.id)
            else:
                self.targets_old.add(target.id)
                target.id = self.targets_map.get(target.id, target.id)
                self.targets_new.add(target.id)

        self.generic_visit(node)
        return node

    def visit_Compare(self, node):
        # 比较符的左右也可能是变量，要处理
        if isinstance(node.left, ast.Name):
            self.args_old.add(node.left.id)
            node.left.id = self.args_map.get(node.left.id, node.left.id)
            self.args_new.add(node.left.id)
        for com in node.comparators:
            if isinstance(com, ast.Name):
                self.args_old.add(com.id)
                com.id = self.args_map.get(com.id, com.id)
                self.args_new.add(com.id)

        # OPEN==CLOSE，要转成Eq
        if isinstance(node.ops[0], ast.Eq):
            # 等号会直接比较变成False
            node = ast.Call(
                func=ast.Name(id='Eq', ctx=ast.Load()),
                args=[node.left, node.comparators[0]],
                keywords=[],
            )

        self.generic_visit(node)
        return node

    def visit_IfExp(self, node):
        # 三元表达式。需要在外部提前替换成if else
        # OPEN>=CLOSE?1:0
        # OPEN>CLOSE?A==B?3:DE>FG?5:6:0
        node = ast.Call(
            func=ast.Name(id='if_else', ctx=ast.Load()),
            args=[node.body, node.test, node.orelse],
            keywords=[],
        )

        self.generic_visit(node)
        return node

    def visit_BinOp(self, node):
        # TypeError: unsupported operand type(s) for *: 'StrictLessThan' and 'int'
        if isinstance(node.op, ast.Mult):
            # (OPEN < CLOSE) * -1
            if isinstance(node.left, ast.Compare):
                node.left = ast.Call(
                    func=ast.Name(id='if_else', ctx=ast.Load()),
                    args=[node.left, ast.Constant(value=1), ast.Constant(value=0)],
                    keywords=[],
                )
            # -1*(OPEN < CLOSE)
            if isinstance(node.right, ast.Compare):
                node.right = ast.Call(
                    func=ast.Name(id='if_else', ctx=ast.Load()),
                    args=[node.right, ast.Constant(value=1), ast.Constant(value=0)],
                    keywords=[],
                )
            # 这种情况要处理吗？
            # (OPEN < CLOSE)*(OPEN < CLOSE)

        if isinstance(node.left, ast.Name):
            self.args_old.add(node.left.id)
            node.left.id = self.args_map.get(node.left.id, node.left.id)
            self.args_new.add(node.left.id)
        if isinstance(node.right, ast.Name):
            self.args_old.add(node.right.id)
            node.right.id = self.args_map.get(node.right.id, node.right.id)
            self.args_new.add(node.right.id)

        self.generic_visit(node)
        return node


def sources_to_asts(*sources):
    """输入多份源代码"""
    raw = []
    assigns = {}
    funcs_new, args_new, targets_new = set(), set(), set()
    for arg in sources:
        r, a, funcs_, args_, targets_ = _source_to_asts(arg)
        raw.append(r)
        assigns.update(a)
        funcs_new.update(funcs_)
        args_new.update(args_)
        targets_new.update(targets_)
    return '\n'.join(raw), assigns, funcs_new, args_new, targets_new


def source_replace(source):
    # 三元表达式转换成 错误版if( )else，一定得在Transformer中修正
    num = 1
    while num > 0:
        # A == B?D == E?1: 2:0 + 0
        # 其实会导致?与:错配，但无所谓，只要多执行几次即可
        source, num = re.subn(r'\?(.+?):(.+?)', r' if( \1 )else \2', source, flags=re.S)
        # break
    # 异或转成乘方，或、与
    source = source.replace('^', '**').replace('||', '|').replace('&&', '&')
    return source


def _source_to_asts(source):
    """源代码"""
    tree = ast.parse(source_replace(source))
    t = SympyTransformer()
    t.visit(tree)

    raw = []
    assigns = []

    if isinstance(tree.body[0], ast.FunctionDef):
        body = tree.body[0].body
    else:
        body = tree.body

    for node in body:
        # 特殊处理的节点
        if isinstance(node, ast.Assign):
            assigns.append(node)
            continue
        # TODO 是否要把其它语句也加入？是否有安全问题？
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raw.append(node)
            continue
    return raw_to_code(raw), assigns_to_dict(assigns), t.funcs_new, t.args_new, t.targets_new


def assigns_to_dict(assigns):
    """赋值表达式转成字典"""
    return {ast.unparse(a.targets): ast.unparse(a.value) for a in assigns}


def raw_to_code(raw):
    """导入语句转字符列表"""
    return '\n'.join([ast.unparse(a) for a in raw])


def sources_to_exprs(globals_, *sources):
    """将源代码转换成表达式"""
    raw, assigns, funcs_new, args_new, targets_new = sources_to_asts(*sources)
    register_symbols(funcs_new, globals_, is_function=True)
    register_symbols(args_new, globals_, is_function=False)
    register_symbols(targets_new, globals_, is_function=False)
    exprs_dict = dict_to_exprs(assigns, globals_)
    return raw, exprs_dict
