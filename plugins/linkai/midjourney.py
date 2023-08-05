from enum import Enum
from config import conf
from common.log import logger
import requests
import threading
import time
from bridge.reply import Reply, ReplyType
import aiohttp
import asyncio
from bridge.context import ContextType
from plugins import EventContext, EventAction

INVALID_REQUEST = 410

class TaskType(Enum):
    GENERATE = "generate"
    UPSCALE = "upscale"
    VARIATION = "variation"
    RESET = "reset"


class Status(Enum):
    PENDING = "pending"
    FINISHED = "finished"
    EXPIRED = "expired"
    ABORTED = "aborted"

    def __str__(self):
        return self.name


class TaskMode(Enum):
    FAST = "fast"
    RELAX = "relax"


class MJTask:
    def __init__(self, id, user_id: str, task_type: TaskType, raw_prompt=None, expires: int = 60 * 30,
                 status=Status.PENDING):
        self.id = id
        self.user_id = user_id
        self.task_type = task_type
        self.raw_prompt = raw_prompt
        self.send_func = None  # send_func(img_url)
        self.expiry_time = time.time() + expires
        self.status = status
        self.img_url = None  # url
        self.img_id = None

    def __str__(self):
        return f"id={self.id}, user_id={self.user_id}, task_type={self.task_type}, status={self.status}, img_id={self.img_id}"


# midjourney bot
class MJBot:
    def __init__(self, config):
        self.base_url = "https://api.link-ai.chat/v1/img/midjourney"

        self.headers = {"Authorization": "Bearer " + conf().get("linkai_api_key")}
        self.config = config
        self.tasks = {}
        self.temp_dict = {}
        self.tasks_lock = threading.Lock()
        self.event_loop = asyncio.new_event_loop()

    def judge_mj_task_type(self, e_context: EventContext):
        """
        判断MJ任务的类型
        :param e_context: 上下文
        :return: 任务类型枚举
        """
        if not self.config or not self.config.get("enabled"):
            return None
        trigger_prefix = conf().get("plugin_trigger_prefix", "$")
        context = e_context['context']
        if context.type == ContextType.TEXT:
            cmd_list = context.content.split(maxsplit=1)
            if cmd_list[0].lower() == f"{trigger_prefix}mj":
                return TaskType.GENERATE
            elif cmd_list[0].lower() == f"{trigger_prefix}mju":
                return TaskType.UPSCALE
        elif context.type == ContextType.IMAGE_CREATE and self.config.get("use_image_create_prefix"):
            return TaskType.GENERATE

    def process_mj_task(self, mj_type: TaskType, e_context: EventContext):
        """
        处理mj任务
        :param mj_type: mj任务类型
        :param e_context: 对话上下文
        """
        context = e_context['context']
        session_id = context["session_id"]
        cmd = context.content.split(maxsplit=1)
        if len(cmd) == 1 and context.type == ContextType.TEXT:
            self._set_reply_text(self.get_help_text(verbose=True), e_context, level=ReplyType.INFO)
            return

        if not self._check_rate_limit(session_id, e_context):
            logger.warn("[MJ] midjourney task exceed rate limit")
            return

        if mj_type == TaskType.GENERATE:
            if context.type == ContextType.IMAGE_CREATE:
                raw_prompt = context.content
            else:
                # 图片生成
                raw_prompt = cmd[1]
            reply = self.generate(raw_prompt, session_id, e_context)
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS
            return

        elif mj_type == TaskType.UPSCALE:
            # 图片放大
            clist = cmd[1].split()
            if len(clist) < 2:
                self._set_reply_text(f"{cmd[0]} 命令缺少参数", e_context)
                return
            img_id = clist[0]
            index = int(clist[1])
            if index < 1 or index > 4:
                self._set_reply_text(f"图片序号 {index} 错误，应在 1 至 4 之间", e_context)
                return
            key = f"{TaskType.UPSCALE.name}_{img_id}_{index}"
            if self.temp_dict.get(key):
                self._set_reply_text(f"第 {index} 张图片已经放大过了", e_context)
                return
            # 图片放大操作
            reply = self.upscale(session_id, img_id, index, e_context)
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS
            return

        else:
            self._set_reply_text(f"暂不支持该命令", e_context)

    def generate(self, prompt: str, user_id: str, e_context: EventContext) -> Reply:
        """
        图片生成
        :param prompt: 提示词
        :param user_id: 用户id
        :param e_context: 对话上下文
        :return: 任务ID
        """
        logger.info(f"[MJ] image generate, prompt={prompt}")
        mode = self._fetch_mode(prompt)
        body = {"prompt": prompt, "mode": mode, "auto_translate": self.config.get("auto_translate")}
        if not self.config.get("img_proxy"):
            body["img_proxy"] = False
        res = requests.post(url=self.base_url + "/generate", json=body, headers=self.headers, timeout=(5, 40))
        if res.status_code == 200:
            res = res.json()
            logger.debug(f"[MJ] image generate, res={res}")
            if res.get("code") == 200:
                task_id = res.get("data").get("task_id")
                real_prompt = res.get("data").get("real_prompt")
                if mode == TaskMode.RELAX.value:
                    time_str = "1~10分钟"
                else:
                    time_str = "1分钟"
                content = f"🚀您的作品将在{time_str}左右完成，请耐心等待\n- - - - - - - - -\n"
                if real_prompt:
                    content += f"初始prompt: {prompt}\n转换后prompt: {real_prompt}"
                else:
                    content += f"prompt: {prompt}"
                reply = Reply(ReplyType.INFO, content)
                task = MJTask(id=task_id, status=Status.PENDING, raw_prompt=prompt, user_id=user_id,
                              task_type=TaskType.GENERATE)
                # put to memory dict
                self.tasks[task.id] = task
                # asyncio.run_coroutine_threadsafe(self.check_task(task, e_context), self.event_loop)
                self._do_check_task(task, e_context)
                return reply
        else:
            res_json = res.json()
            logger.error(f"[MJ] generate error, msg={res_json.get('message')}, status_code={res.status_code}")
            if res.status_code == INVALID_REQUEST:
                reply = Reply(ReplyType.ERROR, "图片生成失败，请检查提示词参数或内容")
            else:
                reply = Reply(ReplyType.ERROR, "图片生成失败，请稍后再试")
            return reply

    def upscale(self, user_id: str, img_id: str, index: int, e_context: EventContext) -> Reply:
        logger.info(f"[MJ] image upscale, img_id={img_id}, index={index}")
        body = {"type": TaskType.UPSCALE.name, "img_id": img_id, "index": index}
        if not self.config.get("img_proxy"):
            body["img_proxy"] = False
        res = requests.post(url=self.base_url + "/operate", json=body, headers=self.headers, timeout=(5, 40))
        logger.debug(res)
        if res.status_code == 200:
            res = res.json()
            if res.get("code") == 200:
                task_id = res.get("data").get("task_id")
                logger.info(f"[MJ] image upscale processing, task_id={task_id}")
                content = f"🔎图片正在放大中，请耐心等待"
                reply = Reply(ReplyType.INFO, content)
                task = MJTask(id=task_id, status=Status.PENDING, user_id=user_id, task_type=TaskType.UPSCALE)
                # put to memory dict
                self.tasks[task.id] = task
                key = f"{TaskType.UPSCALE.name}_{img_id}_{index}"
                self.temp_dict[key] = True
                # asyncio.run_coroutine_threadsafe(self.check_task(task, e_context), self.event_loop)
                self._do_check_task(task, e_context)
                return reply
        else:
            error_msg = ""
            if res.status_code == 461:
                error_msg = "请输入正确的图片ID"
            res_json = res.json()
            logger.error(f"[MJ] upscale error, msg={res_json.get('message')}, status_code={res.status_code}")
            reply = Reply(ReplyType.ERROR, error_msg or "图片生成失败，请稍后再试")
            return reply

    def check_task_sync(self, task: MJTask, e_context: EventContext):
        logger.debug(f"[MJ] start check task status, {task}")
        max_retry_times = 90
        while max_retry_times > 0:
            time.sleep(10)
            url = f"{self.base_url}/tasks/{task.id}"
            try:
                res = requests.get(url, headers=self.headers, timeout=8)
                if res.status_code == 200:
                    res_json = res.json()
                    logger.debug(f"[MJ] task check res sync, task_id={task.id}, status={res.status_code}, "
                                 f"data={res_json.get('data')}, thread={threading.current_thread().name}")
                    if res_json.get("data") and res_json.get("data").get("status") == Status.FINISHED.name:
                        # process success res
                        if self.tasks.get(task.id):
                            self.tasks[task.id].status = Status.FINISHED
                        self._process_success_task(task, res_json.get("data"), e_context)
                        return
                    max_retry_times -= 1
                else:
                    res_json = res.json()
                    logger.warn(f"[MJ] image check error, status_code={res.status_code}, res={res_json}")
                    max_retry_times -= 20
            except Exception as e:
                max_retry_times -= 20
                logger.warn(e)
        logger.warn("[MJ] end from poll")
        if self.tasks.get(task.id):
            self.tasks[task.id].status = Status.EXPIRED

    async def check_task_async(self, task: MJTask, e_context: EventContext):
        try:
            logger.debug(f"[MJ] start check task status, {task}")
            max_retry_times = 90
            while max_retry_times > 0:
                await asyncio.sleep(10)
                async with aiohttp.ClientSession() as session:
                    url = f"{self.base_url}/tasks/{task.id}"
                    try:
                        async with session.get(url, headers=self.headers) as res:
                            if res.status == 200:
                                res_json = await res.json()
                                logger.debug(f"[MJ] task check res, task_id={task.id}, status={res.status}, "
                                             f"data={res_json.get('data')}, thread={threading.current_thread().name}")
                                if res_json.get("data") and res_json.get("data").get("status") == Status.FINISHED.name:
                                    # process success res
                                    if self.tasks.get(task.id):
                                        self.tasks[task.id].status = Status.FINISHED
                                    self._process_success_task(task, res_json.get("data"), e_context)
                                    return
                            else:
                                res_json = await res.json()
                                logger.warn(f"[MJ] image check error, status_code={res.status}, res={res_json}")
                                max_retry_times -= 20
                    except Exception as e:
                        max_retry_times -= 20
                        logger.warn(e)
                max_retry_times -= 1
            logger.warn("[MJ] end from poll")
            if self.tasks.get(task.id):
                self.tasks[task.id].status = Status.EXPIRED
        except Exception as e:
            logger.error(e)

    def _do_check_task(self, task: MJTask, e_context: EventContext):
        threading.Thread(target=self.check_task_sync, args=(task, e_context)).start()

    def _process_success_task(self, task: MJTask, res: dict, e_context: EventContext):
        """
        处理任务成功的结果
        :param task: MJ任务
        :param res: 请求结果
        :param e_context: 对话上下文
        """
        # channel send img
        task.status = Status.FINISHED
        task.img_id = res.get("img_id")
        task.img_url = res.get("img_url")
        logger.info(f"[MJ] task success, task_id={task.id}, img_id={task.img_id}, img_url={task.img_url}")

        # send img
        reply = Reply(ReplyType.IMAGE_URL, task.img_url)
        channel = e_context["channel"]
        channel._send(reply, e_context["context"])

        # send info
        trigger_prefix = conf().get("plugin_trigger_prefix", "$")
        text = ""
        if task.task_type == TaskType.GENERATE:
            text = f"🎨绘画完成!\nprompt: {task.raw_prompt}\n- - - - - - - - -\n图片ID: {task.img_id}"
            text += f"\n\n🔎可使用 {trigger_prefix}mju 命令放大指定图片\n"
            text += f"例如：\n{trigger_prefix}mju {task.img_id} 1"
            reply = Reply(ReplyType.INFO, text)
            channel._send(reply, e_context["context"])

        self._print_tasks()
        return

    def _check_rate_limit(self, user_id: str, e_context: EventContext) -> bool:
        """
        midjourney任务限流控制
        :param user_id: 用户id
        :param e_context: 对话上下文
        :return: 任务是否能够生成, True:可以生成, False: 被限流
        """
        tasks = self.find_tasks_by_user_id(user_id)
        task_count = len([t for t in tasks if t.status == Status.PENDING])
        if task_count >= self.config.get("max_tasks_per_user"):
            reply = Reply(ReplyType.INFO, "您的Midjourney作图任务数已达上限，请稍后再试")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            return False
        task_count = len([t for t in self.tasks.values() if t.status == Status.PENDING])
        if task_count >= self.config.get("max_tasks"):
            reply = Reply(ReplyType.INFO, "Midjourney作图任务数已达上限，请稍后再试")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            return False
        return True

    def _fetch_mode(self, prompt) -> str:
        mode = self.config.get("mode")
        if "--relax" in prompt or mode == TaskMode.RELAX.value:
            return TaskMode.RELAX.value
        return mode or TaskMode.FAST.value

    def _run_loop(self, loop: asyncio.BaseEventLoop):
        """
        运行事件循环，用于轮询任务的线程
        :param loop: 事件循环
        """
        loop.run_forever()
        loop.stop()

    def _print_tasks(self):
        for id in self.tasks:
            logger.debug(f"[MJ] current task: {self.tasks[id]}")

    def _set_reply_text(self, content: str, e_context: EventContext, level: ReplyType = ReplyType.ERROR):
        """
        设置回复文本
        :param content: 回复内容
        :param e_context: 对话上下文
        :param level: 回复等级
        """
        reply = Reply(level, content)
        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS

    def get_help_text(self, verbose=False, **kwargs):
        trigger_prefix = conf().get("plugin_trigger_prefix", "$")
        help_text = "🎨利用Midjourney进行画图\n\n"
        if not verbose:
            return help_text
        help_text += f" - 生成: {trigger_prefix}mj 描述词1, 描述词2.. \n - 放大: {trigger_prefix}mju 图片ID 图片序号\n\n例如：\n\"{trigger_prefix}mj a little cat, white --ar 9:16\"\n\"{trigger_prefix}mju 1105592717188272288 2\""

        return help_text

    def find_tasks_by_user_id(self, user_id) -> list:
        result = []
        with self.tasks_lock:
            now = time.time()
            for task in self.tasks.values():
                if task.status == Status.PENDING and now > task.expiry_time:
                    task.status = Status.EXPIRED
                    logger.info(f"[MJ] {task} expired")
                if task.user_id == user_id:
                    result.append(task)
        return result


def check_prefix(content, prefix_list):
    if not prefix_list:
        return None
    for prefix in prefix_list:
        if content.startswith(prefix):
            return prefix
    return None
