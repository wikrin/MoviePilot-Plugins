from bs4 import BeautifulSoup


def form(sites_options: list[dict], is_v2: bool = True) -> list:
    return [
        {
            'component': 'VForm',
            'content': [
                {
                    'component': 'VRow',
                    'content': [
                        {
                            'component': 'VCol',
                            'props': {'cols': 12, 'md': 3},
                            'content': [
                                {
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'enabled',
                                        'label': '启用插件',
                                    },
                                }
                            ],
                        },
                        {
                            'component': 'VCol',
                            'props': {'cols': 12, 'md': 3},
                            'content': [
                                {
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'notify',
                                        'label': '自动取消订阅并通知',
                                    },
                                }
                            ],
                        },
                        {
                            'component': 'VCol',
                            'props': {'cols': 12, 'md': 3},
                            'content': [
                                {
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'total_change',
                                        'label': '不更新元数据',
                                    },
                                }
                            ],
                        },
                        {
                            'component': 'VCol',
                            'props': {'cols': 12, 'md': 3},
                            'content': [
                                {
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'onlyonce',
                                        'label': '立即运行一次',
                                    },
                                }
                            ],
                        },
                    ],
                },
                {
                    'component': 'VRow',
                    'content': [
                        {
                            'component': 'VCol',
                            'props': {'cols': 8, 'md': 4},
                            'content': [
                                {
                                    # 'component': 'VTextField', # 组件替换为VCronField
                                    'component': 'VCronField',
                                    'props': {
                                        'model': 'cron',
                                        'label': '执行周期',
                                        'placeholder': '5位cron表达式，留空自动',
                                    },
                                }
                            ],
                        },
                        {
                            'component': 'VCol',
                            'props': {'cols': 8, 'md': 4},
                            'content': [
                                {
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'uid',
                                        'label': 'UID/用户名',
                                        'placeholder': '设置了用户名填写用户名，否则填写UID',
                                    },
                                },
                            ],
                        },
                        {
                            'component': 'VCol',
                            'props': {'cols': 8, 'md': 4},
                            'content': [
                                {
                                    'component': 'VSelect',
                                    'props': {
                                        'model': 'collection_type',
                                        'label': '收藏类型',
                                        'chips': True,
                                        'multiple': True,
                                        'items': [
                                            {'title': '在看', 'value': 3},
                                            {'title': '想看', 'value': 1},
                                        ],
                                    },
                                }
                            ],
                        },
                    ],
                },
                {
                    'component': 'VRow',
                    'content': [
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 12,
                            },
                            'content': [
                                {
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'info',
                                        'variant': 'tonal',
                                    },
                                    'content': parse_html(
                                        '<p>提示： <strong>剧集组优先级</strong>越靠前优先级越高。</p>'
                                    ),
                                },
                            ],
                        },
                    ],
                },
                {
                    'component': 'VRow',
                    'content': [
                        {
                            'component': 'VCol',
                            'props': {'cols': 8, 'md': 4},
                            'content': [
                                {
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'match_groups',
                                        'disabled': not is_v2,
                                        'label': '剧集组填充(实验性)',
                                    }
                                }
                            ]
                        },
                        {
                            'component': 'VCol',
                            'props': {'cols': 8},
                            'content': [
                                {
                                    'component': 'VSelect',
                                    'props': {
                                        'model': 'group_select_order',
                                        'label': '剧集组优先级',
                                        'disabled': not is_v2,
                                        'chips': True,
                                        'multiple': True,
                                        'clearable': True,
                                        'items': [
                                            {"title": "初始播出日期", "value": 1},
                                            {"title": "绝对", "value": 2},
                                            {"title": "DVD", "value": 3},
                                            {"title": "数字", "value": 4},
                                            {"title": "故事线", "value": 5},
                                            {"title": "制片", "value": 6},
                                            {"title": "电视", "value": 7},
                                        ],
                                    },
                                }
                            ]
                        },
                    ]
                },
                {
                    'component': 'VRow',
                    'content': [
                        {
                            'component': 'VCol',
                            'props': {'cols': 12, 'md': 6},
                            'content': [
                                {
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'save_path',
                                        'label': '保存目录',
                                        'placeholder': '留空自动',
                                    },
                                }
                            ],
                        },
                        {
                            'component': 'VCol',
                            'props': {'cols': 12, 'md': 6},
                            'content': [
                                {
                                    'component': 'VSelect',
                                    'props': {
                                        'model': 'sites',
                                        'label': '选择站点',
                                        'chips': True,
                                        'multiple': True,
                                        'clearable': True,
                                        'items': sites_options,
                                    },
                                }
                            ],
                        },
                    ],
                },
                {
                    'component': 'VRow',
                    'content': [
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 12,
                            },
                            'content': [
                                {
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'info',
                                        'variant': 'tonal',
                                    },
                                    'content': parse_html(
                                        '<p>注意： 该插件仅会将<strong>公开</strong>的收藏添加到<strong>订阅</strong>。</p>'
                                    ),
                                }
                            ],
                        }
                    ],
                },
                {
                    'component': 'VRow',
                    'content': [
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 12,
                            },
                            'content': [
                                {
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'info',
                                        'variant': 'tonal',
                                    },
                                    'content': parse_html(
                                        '<p>注意： 开启<strong>自动取消订阅并通知</strong>后，已添加的订阅在下一次执行时若不在已选择的<strong>收藏类型</strong>中，将会被取消订阅。</p>'
                                    ),
                                }
                            ],
                        }
                    ],
                },
            ],
        },
        {
            'component': 'VRow',
            'content': [
                {
                    'component': 'VCol',
                    'props': {
                        'cols': 12,
                    },
                    'content': [
                        {
                            'component': 'VAlert',
                            'props': {
                                'type': 'info',
                                'variant': 'tonal',
                            },
                            'content': parse_html(
                                '<p>注意： 开启<strong>不更新元数据</strong>后，从<a href="https://bangumi.github.io/api/#/%E7%AB%A0%E8%8A%82/getEpisodes" target="_blank"><u>Bangumi API</u></a>获取到总集数将不会因<strong>订阅元数据更新</strong>改变。</p>'
                            ),
                        },
                    ],
                },
            ],
        },
    ], {
        "enabled": False,
        "total_change": False,
        "notify": False,
        "onlyonce": False,
        "cron": "",
        "uid": "",
        "collection_type": [3],
        "save_path": "",
        "sites": [],
        "match_groups": False,
        "group_select_order": [],
    }


def parse_html(html_string: str) -> list:
    soup = BeautifulSoup(html_string, 'html.parser')
    result: list = []

    # 定义需要直接转为文本的标签
    inline_text_tags = {'strong', 'u', 'em', 'b', 'i'}

    def process_element(element: BeautifulSoup):
        # 处理纯文本节点
        if element.name is None:
            text = element.strip()
            return text if text else ""

        # 处理HTML标签
        component = element.name
        props = {attr: element[attr] for attr in element.attrs}
        content = []

        # 递归处理子元素
        for child in element.children:
            child_content = process_element(child)
            if isinstance(child_content, str):
                content.append({'component': 'span', 'text': child_content})
            elif child_content:  # 只有在child_content不为空时添加
                content.append(child_content)

        # 构建标签对象
        tag_data = {
            'component': component,
            'props': props,
            'content': content if component not in inline_text_tags else [],
        }

        if content and component in inline_text_tags:
            tag_data['text'] = ' '.join(
                item['text'] for item in content if 'text' in item
            )

        return tag_data

    # 遍历所有子元素
    for element in soup.children:
        element_content = process_element(element)
        if element_content:  # 只增加非空内容
            result.append(element_content)

    return result
