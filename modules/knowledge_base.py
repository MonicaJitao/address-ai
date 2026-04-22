# ============================================================
# 中国地址知识库
# 包含：省份英文映射、主要城市映射、城市归属省份索引、
#       道路后缀缩写规则、楼宇类型词汇
# ============================================================

# ── 省级行政区 → 标准英文名称 ─────────────────────────────
# 直辖市、省份、自治区、特别行政区全量覆盖
PROVINCE_MAP: dict[str, str] = {
    # 直辖市
    "北京": "Beijing",      "北京市": "Beijing",
    "天津": "Tianjin",      "天津市": "Tianjin",
    "上海": "Shanghai",     "上海市": "Shanghai",
    "重庆": "Chongqing",    "重庆市": "Chongqing",
    # 省份
    "河北": "Hebei",        "河北省": "Hebei",
    "山西": "Shanxi",       "山西省": "Shanxi",
    "辽宁": "Liaoning",     "辽宁省": "Liaoning",
    "吉林": "Jilin",        "吉林省": "Jilin",
    "黑龙江": "Heilongjiang","黑龙江省": "Heilongjiang",
    "江苏": "Jiangsu",      "江苏省": "Jiangsu",
    "浙江": "Zhejiang",     "浙江省": "Zhejiang",
    "安徽": "Anhui",        "安徽省": "Anhui",
    "福建": "Fujian",       "福建省": "Fujian",
    "江西": "Jiangxi",      "江西省": "Jiangxi",
    "山东": "Shandong",     "山东省": "Shandong",
    "河南": "Henan",        "河南省": "Henan",
    "湖北": "Hubei",        "湖北省": "Hubei",
    "湖南": "Hunan",        "湖南省": "Hunan",
    "广东": "Guangdong",    "广东省": "Guangdong",
    "海南": "Hainan",       "海南省": "Hainan",
    "四川": "Sichuan",      "四川省": "Sichuan",
    "贵州": "Guizhou",      "贵州省": "Guizhou",
    "云南": "Yunnan",       "云南省": "Yunnan",
    "陕西": "Shaanxi",      "陕西省": "Shaanxi",
    "甘肃": "Gansu",        "甘肃省": "Gansu",
    "青海": "Qinghai",      "青海省": "Qinghai",
    # 自治区
    "内蒙古": "Inner Mongolia", "内蒙古自治区": "Inner Mongolia",
    "广西": "Guangxi",          "广西壮族自治区": "Guangxi",
    "西藏": "Tibet",            "西藏自治区": "Tibet",
    "宁夏": "Ningxia",          "宁夏回族自治区": "Ningxia",
    "新疆": "Xinjiang",         "新疆维吾尔自治区": "Xinjiang",
    # 特别行政区
    "香港": "Hong Kong",        "香港特别行政区": "Hong Kong",
    "澳门": "Macao",            "澳门特别行政区": "Macao",
    "台湾": "Taiwan",           "台湾省": "Taiwan",
}

# ── 主要城市 → 标准英文名称 ───────────────────────────────
CITY_MAP: dict[str, str] = {
    "深圳": "Shenzhen",     "深圳市": "Shenzhen",
    "广州": "Guangzhou",    "广州市": "Guangzhou",
    "杭州": "Hangzhou",     "杭州市": "Hangzhou",
    "南京": "Nanjing",      "南京市": "Nanjing",
    "武汉": "Wuhan",        "武汉市": "Wuhan",
    "成都": "Chengdu",      "成都市": "Chengdu",
    "西安": "Xi'an",        "西安市": "Xi'an",
    "苏州": "Suzhou",       "苏州市": "Suzhou",
    "青岛": "Qingdao",      "青岛市": "Qingdao",
    "宁波": "Ningbo",       "宁波市": "Ningbo",
    "沈阳": "Shenyang",     "沈阳市": "Shenyang",
    "大连": "Dalian",       "大连市": "Dalian",
    "厦门": "Xiamen",       "厦门市": "Xiamen",
    "福州": "Fuzhou",       "福州市": "Fuzhou",
    "长沙": "Changsha",     "长沙市": "Changsha",
    "郑州": "Zhengzhou",    "郑州市": "Zhengzhou",
    "合肥": "Hefei",        "合肥市": "Hefei",
    "济南": "Jinan",        "济南市": "Jinan",
    "哈尔滨": "Harbin",     "哈尔滨市": "Harbin",
    "长春": "Changchun",    "长春市": "Changchun",
    "昆明": "Kunming",      "昆明市": "Kunming",
    "南宁": "Nanning",      "南宁市": "Nanning",
    "南昌": "Nanchang",     "南昌市": "Nanchang",
    "太原": "Taiyuan",      "太原市": "Taiyuan",
    "石家庄": "Shijiazhuang","石家庄市": "Shijiazhuang",
    "贵阳": "Guiyang",      "贵阳市": "Guiyang",
    "兰州": "Lanzhou",      "兰州市": "Lanzhou",
    "银川": "Yinchuan",     "银川市": "Yinchuan",
    "西宁": "Xining",       "西宁市": "Xining",
    "乌鲁木齐": "Urumqi",   "乌鲁木齐市": "Urumqi",
    "拉萨": "Lhasa",        "拉萨市": "Lhasa",
    "呼和浩特": "Hohhot",   "呼和浩特市": "Hohhot",
    "南通": "Nantong",      "南通市": "Nantong",
    "无锡": "Wuxi",         "无锡市": "Wuxi",
    "珠海": "Zhuhai",       "珠海市": "Zhuhai",
    "佛山": "Foshan",       "佛山市": "Foshan",
    "东莞": "Dongguan",     "东莞市": "Dongguan",
    "温州": "Wenzhou",      "温州市": "Wenzhou",
    "烟台": "Yantai",       "烟台市": "Yantai",
    "徐州": "Xuzhou",       "徐州市": "Xuzhou",
    "常州": "Changzhou",    "常州市": "Changzhou",
}

# ── 城市所属省份（用于知识库层交叉校验） ──────────────────
# 格式：城市英文名 → 所属省份英文名
CITY_PROVINCE_MAP: dict[str, str] = {
    "Shenzhen": "Guangdong",    "Guangzhou": "Guangdong",
    "Hangzhou": "Zhejiang",     "Ningbo": "Zhejiang",
    "Wenzhou": "Zhejiang",      "Nanjing": "Jiangsu",
    "Suzhou": "Jiangsu",        "Wuxi": "Jiangsu",
    "Nantong": "Jiangsu",       "Changzhou": "Jiangsu",
    "Xuzhou": "Jiangsu",        "Wuhan": "Hubei",
    "Chengdu": "Sichuan",       "Xi'an": "Shaanxi",
    "Qingdao": "Shandong",      "Jinan": "Shandong",
    "Yantai": "Shandong",       "Shenyang": "Liaoning",
    "Dalian": "Liaoning",       "Xiamen": "Fujian",
    "Fuzhou": "Fujian",         "Changsha": "Hunan",
    "Zhengzhou": "Henan",       "Hefei": "Anhui",
    "Harbin": "Heilongjiang",   "Changchun": "Jilin",
    "Kunming": "Yunnan",        "Nanning": "Guangxi",
    "Nanchang": "Jiangxi",      "Taiyuan": "Shanxi",
    "Shijiazhuang": "Hebei",    "Guiyang": "Guizhou",
    "Lanzhou": "Gansu",         "Yinchuan": "Ningxia",
    "Xining": "Qinghai",        "Urumqi": "Xinjiang",
    "Lhasa": "Tibet",           "Hohhot": "Inner Mongolia",
    "Zhuhai": "Guangdong",      "Foshan": "Guangdong",
    "Dongguan": "Guangdong",
    # 直辖市（城市即省份）
    "Beijing": "Beijing",       "Tianjin": "Tianjin",
    "Shanghai": "Shanghai",     "Chongqing": "Chongqing",
}

# ── 道路类型后缀 → 标准英文缩写 ──────────────────────────
ROAD_SUFFIX_MAP: dict[str, str] = {
    "路": "Rd.",
    "大道": "Ave.",
    "街": "St.",
    "巷": "Lane",
    "弄": "Alley",
    "胡同": "Hutong",
    "道": "Rd.",
    "公路": "Hwy.",
    "大街": "Blvd.",
    "环路": "Ring Rd.",
    "高速": "Expy.",
}

# ── 楼宇类型词汇 → 标准英文表达 ──────────────────────────
BUILDING_TYPE_MAP: dict[str, str] = {
    "大厦": "Tower",
    "广场": "Plaza",
    "中心": "Center",
    "大楼": "Bldg.",
    "科技园": "Science Park",
    "产业园": "Industrial Park",
    "工业园": "Industrial Park",
    "园区": "Park",
    "创业园": "Innovation Park",
    "商务中心": "Business Center",
    "写字楼": "Office Bldg.",
    "办公楼": "Office Bldg.",
    "综合体": "Complex",
    "商城": "Mall",
    "孵化器": "Incubator",
    "加速器": "Accelerator",
}

# ── 已知有官方英文名的地标/园区（优先级高于 LLM 翻译） ────
OFFICIAL_NAME_MAP: dict[str, str] = {
    "腾讯大厦": "Tencent Building",
    "腾讯滨海大厦": "Tencent Binhai Building",
    "华为坂田基地": "Huawei Bantian Base",
    "阿里巴巴西溪园区": "Alibaba Xixi Campus",
    "字节跳动大厦": "ByteDance Tower",
    "国家高新技术产业开发区": "National Hi-Tech Industrial Development Zone",
    "科技园": "Science Park",
    "南山科技园": "Nanshan Science Park",
    "软件产业基地": "Software Industrial Base",
}


def lookup_province(name: str) -> str | None:
    """查找省份标准英文名；若找不到则返回 None"""
    return PROVINCE_MAP.get(name) or PROVINCE_MAP.get(name.rstrip("省市区"))


def lookup_city(name: str) -> str | None:
    """查找城市标准英文名；若找不到则返回 None"""
    return CITY_MAP.get(name) or CITY_MAP.get(name.rstrip("市"))


def verify_city_province(city_en: str, province_en: str) -> bool:
    """
    校验城市与省份是否在同一行政区域内。
    若城市不在索引中（小城市），则跳过校验，返回 True。
    """
    expected_province = CITY_PROVINCE_MAP.get(city_en)
    if expected_province is None:
        # 小城市不在索引中，无法校验，默认通过
        return True
    return expected_province == province_en


def get_official_name(zh_name: str) -> str | None:
    """查找地标/园区的官方英文名称；优先级高于 LLM 翻译"""
    return OFFICIAL_NAME_MAP.get(zh_name)
