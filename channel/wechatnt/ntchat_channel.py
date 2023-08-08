import io
import random
import threading
import uuid
import xml.dom.minidom
import requests
import ntchat
from PIL import Image
from bridge.context import *
from bridge.reply import *
from channel.chat_channel import ChatChannel
from common.singleton import singleton
from common.log import logger
from common.time_check import time_checker
from config import conf
from bridge.bridge import Bridge
from plugins.plugin_manager import PluginManager
from plugins import *
import sys
import os
import time
from channel.wechatnt.ntchat_tool import *

#全局变量
os.environ['ntchat_LOG'] = "ERROR"
wechatnt = ntchat.WeChat()
ntchat_channel = None


# 注册好友请求监听
@wechatnt.msg_register(ntchat.MT_RECV_FRIEND_MSG)
def on_recv_text_msg(wechat_instance: ntchat.WeChat, message):
    xml_content = message["data"]["raw_msg"]
    dom = xml.dom.minidom.parseString(xml_content)

    # 从xml取相关参数
    encryptusername = dom.documentElement.getAttribute("encryptusername")
    ticket = dom.documentElement.getAttribute("ticket")
    scene = dom.documentElement.getAttribute("scene")
    
    #是否有开启自动通过好友设置
    accept_friend = False
    try:
        accept_friend = conf().get("accept_friend", False)
    except Exception as e:
        logger.error(f"配置文件未定义 accept_friend")
        
    if accept_friend:
        # 自动同意好友申请
        delay = random.randint(1, 180)
        threading.Timer(delay, wechat_instance.accept_friend_request,
                        args=(encryptusername, ticket, int(scene))).start()
    else:
        logger.info("ntchat未开启自动同意好友申请")
        
    
# 注册消息回调
@wechatnt.msg_register([ntchat.MT_RECV_TEXT_MSG, ntchat.MT_RECV_IMAGE_MSG,
                        ntchat.MT_RECV_VOICE_MSG, ntchat.MT_ROOM_ADD_MEMBER_NOTIFY_MSG,
                        ntchat.MT_RECV_SYSTEM_MSG, ntchat.MT_RECV_OTHER_APP_MSG])
def all_msg_handler(wechat_instance: ntchat.WeChat, message):
    logger.debug(f"收到消息: {message}")

    #登录信息
    login_info = wechatnt.get_login_info()
    nickname = login_info['nickname']
    user_id = login_info['wxid']
    
    #发消息用户ID
    from_wxid = message["data"]["from_wxid"]
    #接受消息用户ID
    to_wxid = message["data"]["to_wxid"]
    
    #如果监听到自回复，跳过
    if from_wxid == to_wxid or from_wxid == user_id:
        logger.debug(f"自回复消息，跳过处理")
        return

    #获取消息处理结果
    context = NTTool(wechat_instance).dealMessage(message)
    content = context.content
    
    #群聊消息
    isGroup = context.get("isgroup")
    if isGroup:
        #是否被@
        is_at = context.get("is_at")
        #群聊消息是否前缀匹配 or 关键词匹配
        match_prefix = check_prefix(content, conf().get("group_chat_prefix"))
        match_contain = check_contain(content, conf().get("group_chat_keyword"))
        is_deal_msg = is_at or match_prefix or match_contain
        if not is_deal_msg:
            logger.debug(f"非@机器人的群聊消息 或 机器人自己发送的消息 或 消息前缀/消息内容未包含配置文件的配置内容，跳过处理")
            return
    
    #回复对象
    reply: Reply = None
    
    #转发消息给插件，优先处理
    try:
        logger.debug(f"即将将消息发送给插件，查询结果")
        #检测插件是否会消费该消息
        e_context = PluginManager().emit_event(
            EventContext(
                Event.ON_HANDLE_CONTEXT,
                {"channel": "ntChat", "context": context, "reply": Reply()},
            )
        )
        if e_context and e_context.is_pass():
            reply = e_context["reply"]
            
    except Exception as e:
        logger.error(f"执行插件任务报错！错误信息为：{e}")
    
    #未命中插件，默认处理
    if reply is None or reply == "" or reply.content is None or reply.content == "":
        logger.debug(f"插件未命中查询，将使用GPT查询结果")
        #画图处理
        img_match_prefix = check_prefix(content, conf().get("image_create_prefix"))
        if img_match_prefix:
            content = content.replace(img_match_prefix, "", 1)
            context.type = ContextType.IMAGE_CREATE
            context.content = content
            
        #查询结果
        reply = Bridge().fetch_reply_content(content, context)
        
    #发消息
    ntchat_channel.send(reply, context)
    

#检查前缀是否匹配        
def check_prefix(content, prefix_list):
    if not prefix_list:
        return None
    for prefix in prefix_list:
        if content.startswith(prefix):
            return prefix
    return None

#检查包含
def check_contain(content, keyword_list):
    if not keyword_list:
        return None
    for ky in keyword_list:
        if content.find(ky) != -1:
            return True
    return None

@singleton
class NtchatChannel(object):

    #init方法
    def __init__(self):
        super().__init__()
        #配置文件
        self.config = conf()
        #tool
        self.tool = NTTool(wechatnt)

    # 初始化
    def startup(self):
        #设置全局
        global ntchat_channel
        ntchat_channel = self
        
        #登录
        logger.info("开始初始化······")
        smart = True
        try:
            smart = self.config.get("ntchat_smart", True)
        except Exception as e:
            logger.error(f"配置文件未定义 ntchat_smart")
        wechatnt.open(smart=smart)
        wechatnt.wait_login()
        logger.info("等待登录······")
        
        #获取登录信息
        login_info = wechatnt.get_login_info()  
        self.user_id = login_info['wxid']
        self.name = login_info['nickname']
        logger.info(f"登录信息:>>>user_id:{self.user_id}>>>>>>>>name:{self.name}，登录信息为：{login_info}")
        
        #处理用户信息
        self.dealUserInfo()

        #进程保活
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            ntchat.exit_()
            os._exit(0)
        
    #处理用户信息
    def dealUserInfo(self):
            
        #获取群聊信息
        contacts = wechatnt.get_contacts()
        rooms = wechatnt.get_rooms()
            
        #处理群聊信息
        result = {}
        # 遍历群聊
        for room in rooms:
            # 获取聊天室ID
            room_wxid = room['wxid']
            # 获取聊天室成员
            room_members = wechatnt.get_room_members(room_wxid)
            # 保存
            result[room_wxid] = room_members
            
        #写入文件
        directoryName = "tmp"
        # 通讯录
        self.tool.writeFile('contacts.json', directoryName, contacts)
        # 群聊
        self.tool.writeFile('rooms.json', directoryName, rooms)
        # 群聊 + 成员
        self.tool.writeFile('room_members.json', directoryName, result)
   

    # 统一的发送函数，每个Channel自行实现，根据reply的type字段发送不同类型的消息
    def send(self, reply: Reply, context: Context):
        #打印对象信息
        logger.debug(f"reply对象 信息为：{vars(reply)}")
        logger.debug(f"context对象 信息为：{vars(context)}")
        
        #回复类型
        type = reply.type
        #回复内容
        reply_text = reply.content
        
        try:
            #收消息id
            receiver = context["receiver"]
            
            #文本消息
            if type == ReplyType.TEXT or type == ReplyType.ERROR or type == ReplyType.INFO:
                msgObj: ChatMessage = context.get("msg")
                if msgObj.is_group:
                    wxid = msgObj.from_user_id
                    wxid_list = [wxid]
                    #增加@
                    at_userName = msgObj.from_user_nickname
                    if at_userName is not None and len(at_userName) > 0:
                        reply_text = "@{at_userName} " + reply_text
                    #回复 
                    wechatnt.send_room_at_msg(receiver, reply_text, wxid_list)
                else:
                    #回复
                    wechatnt.send_text(receiver, reply_text)
                logger.info("[NT-WX]回复 Text消息 - 成功！回复内容为：{}, 消息接收者receiver：{}".format(reply, receiver))
            
            #图片下载    
            elif type == ReplyType.IMAGE_URL:  # 从网络下载图片
                img_url = reply_text
                filename = str(uuid.uuid4())
                image_path = self.download_and_compress_image(img_url, filename)
                wechatnt.send_image(receiver, file_path=image_path)
                logger.info("[NT-WX]回复 网络URL图片 - 回复成功！ 图片url：{}, receiver={}".format(img_url, receiver))
            
            #图片文件    
            elif type == ReplyType.IMAGE:  # 从文件读取图片
                wechatnt.send_image(reply_text, toUserName=receiver)
                logger.info("[NT-WX]回复 图片文件 - 回复成功！ receiver={}".format(receiver))
            
            #视频链接    
            elif type == ReplyType.VIDEO_URL:
                video_url = reply_text
                filename = str(uuid.uuid4())
                # 调用你的函数，下载视频并保存为本地文件
                video_path = self.download_video(video_url, filename)
                if video_path is None:
                    # 如果视频太大，下载可能会被跳过，此时 video_path 将为 None
                    wechatnt.send_text(receiver, "抱歉，视频太大了！！！")
                else:
                    wechatnt.send_video(receiver, video_path)
                logger.info("[NT-WX]回复 网络URL视频 - 回复成功！视频URL为：{}, 消息接收者receiver：{}".format(video_url, receiver))
                
            #名片    
            elif reply.type == ReplyType.CARD:
                wechatnt.send_card(receiver, reply_text)
                logger.info("[NT-WX]回复 名片 - 回复成功！回复内容为：{}, receiver={}".format(reply, receiver))
            
            #邀请进群    
            elif reply.type == ReplyType.InviteRoom:
                member_list = [receiver]
                wechatnt.invite_room_member(reply_text, member_list)
                logger.info("[NT-WX]回复 邀请进群 - 回复成功！回复内容为：{}, receiver={}".format(reply, receiver))
        
        except Exception as e:
            logger.error(f"发送消息失败！错误信息：{e}")   
    
    #图片下载  
    def download_and_compress_image(self, url, filename, quality=80):
        # 确定保存图片的目录
        directory = os.path.join(os.getcwd(), "tmp")
        # 如果目录不存在，则创建目录
        if not os.path.exists(directory):
            os.makedirs(directory)

        # 下载图片
        response = requests.get(url)
        image = Image.open(io.BytesIO(response.content))

        # 压缩图片
        image_path = os.path.join(directory, f"{filename}.jpg")
        image.save(image_path, "JPEG", quality=quality)

        return image_path

    #视频下载
    def download_video(self, url, filename):
        # 确定保存视频的目录
        directory = os.path.join(os.getcwd(), "tmp")
        # 如果目录不存在，则创建目录
        if not os.path.exists(directory):
            os.makedirs(directory)

        # 下载视频
        response = requests.get(url, stream=True)
        total_size = 0

        video_path = os.path.join(directory, f"{filename}.mp4")

        with open(video_path, 'wb') as f:
            for block in response.iter_content(1024):
                total_size += len(block)

                # 如果视频的总大小超过30MB (30 * 1024 * 1024 bytes)，则停止下载并返回
                if total_size > 30 * 1024 * 1024:
                    logger.info("[WX] Video is larger than 30MB, skipping...")
                    return None

                f.write(block)

        return video_path

    #获取名称
    def get_wxid_by_name(self, room_members, group_wxid, name):
        if group_wxid in room_members:
            for member in room_members[group_wxid]['member_list']:
                if member['display_name'] == name or member['nickname'] == name:
                    return member['wxid']
        return None  # 如果没有找到对应的group_wxid或name，则返回None

    
    #检查
    def _check(self, func):
        def wrapper(self, cmsg: ChatMessage):
            msgId = cmsg.msg_id
            create_time = cmsg.create_time  # 消息时间戳
            if create_time is not None:
                if int(create_time) < int(time.time()) - 60:  # 跳过1分钟前的历史消息
                    logger.debug("[WX]history message {} skipped".format(msgId))
                    return
            return func(self, cmsg)

        return wrapper
    
    #确保文件可读
    def ensure_file_ready(self, file_path, timeout=10, interval=0.5):
        """确保文件可读。

        :param file_path: 文件路径。
        :param timeout: 超时时间，单位为秒。
        :param interval: 检查间隔，单位为秒。
        :return: 文件是否可读。
        """
        start_time = time.time()
        while True:
            if os.path.exists(file_path) and os.access(file_path, os.R_OK):
                return True
            elif time.time() - start_time > timeout:
                return False
            else:
                time.sleep(interval)
