"""Versioned screenshot taxonomy; provider mappings are not interchangeable indexes."""
VERSION = '1.0.0'

# Codes were checked against the vendors' public category pages. A mapping change
# requires a new catalog version; no runtime fuzzy matching or concept substitution.
_ROWS = (
    ('semiconductor-materials', '半导体材料', '884091', 'thshy', '884091', 'BK1325', '半导体材料'),
    ('pet-copper', 'PET铜箔', '886020', 'gn', '309030', 'BK1113', '复合集流体'),
    ('cultivated-diamond', '培育钻石', '885937', 'gn', '308774', 'BK1023', '培育钻石'),
    ('semiconductor-equipment', '半导体设备', '884229', 'thshy', '884229', 'BK1326', '半导体设备'),
    ('pcb', 'PCB概念', '885959', 'gn', '308832', 'BK0877', 'PCB'),
    ('advanced-packaging', '先进封装', '886009', 'gn', '309004', 'BK1101', '先进封装'),
    ('components', '元件', '881270', 'thshy', '881270', 'BK0459', '电子元件'),
    ('semiconductor', '半导体', '881121', 'thshy', '881121', 'BK1036', '半导体'),
    ('national-chip-fund', '国家大基金持股', '885893', 'gn', '307816', None, None),
    ('mlcc', 'MLCC概念', '886112', 'gn', '309269', 'BK0890', 'MLCC'),
    ('memory', '存储芯片', '886042', 'gn', '307940', 'BK1137', '存储芯片'),
    ('cpo', '共封装光学（CPO）', None, 'gn', '309049', 'BK1128', 'CPO概念'),
)

SECTORS = []
for ident, label, index_code, kind, ths_code, em_code, em_label in _ROWS:
    SECTORS.append({
        'id': ident, 'label': label, 'ths_index': index_code,
        'ths_kind': kind, 'ths_code': ths_code,
        'ths_url': f'https://q.10jqka.com.cn/{kind}/detail/code/{ths_code}/',
        'em_code': em_code, 'em_label': em_label,
        'em_difference': (
            '复合集流体范围比PET铜箔更宽，并非同花顺PET铜箔指数成分。'
            if ident == 'pet-copper' else
            '没有已核验的对应分类，不以基金重仓替代国家大基金持股。'
            if ident == 'national-chip-fund' else
            '东财近似分类，成分、分类边界与同花顺可能不同，不视为同一指数。'
        ),
    })
