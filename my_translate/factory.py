def create_translator(voice_type):
    if voice_type == "baidu":
        from my_translate.baidu.baidu_translate import BaiduTranslator

        return BaiduTranslator()
    raise RuntimeError
