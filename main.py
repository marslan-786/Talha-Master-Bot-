import os
import sys
import asyncio
import logging
import uuid
import shutil
import signal
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.errors import FloodWait
from motor.motor_asyncio import AsyncIOMotorClient

# ================= CONFIGURATION =================
API_ID = 94575  # اپنا API ID یہاں لکھیں
API_HASH = "a3406de8d171bb422bb6ddf3bbd800e2" # اپنا API HASH یہاں لکھیں
BOT_TOKEN = "8505785410:AAGiDN3FuECbg_K6N_qtjK7OjXh1YYPy5fk" # اپنا BOT TOKEN یہاں لکھیں
MONGO_URL = "mongodb://mongo:AEvrikOWlrmJCQrDTQgfGtqLlwhwLuAA@crossover.proxy.rlwy.net:29609" # ریلوے والا MongoDB URL یہاں لکھیں

# ایک سے زیادہ اونرز کی لسٹ
OWNER_IDS = [8167904992, 7134046678] 

# ================= DATABASE SETUP =================
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["master_bot_db"]
users_col = db["authorized_users"]
keys_col = db["access_keys"]
projects_col = db["projects"]

# ================= GLOBAL VARIABLES =================
# یہ ریم (RAM) میں پروسیسز کو یاد رکھے گا
# Format: {project_id: asyncio.subprocess.Process}
ACTIVE_PROCESSES = {} 

# یوزر سٹیٹ کے لیے (کہ ابھی وہ کیا کر رہا ہے)
USER_STATE = {} 

logging.basicConfig(level=logging.INFO)
app = Client("MasterBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ================= HELPER FUNCTIONS =================

async def is_authorized(user_id):
    if user_id in OWNER_IDS:
        return True
    user = await users_col.find_one({"user_id": user_id})
    return True if user else False

def get_main_menu(user_id):
    btns = [
        [InlineKeyboardButton("🚀 Deploy New Project", callback_data="deploy_new")],
        [InlineKeyboardButton("📂 Manage Projects", callback_data="manage_projects")]
    ]
    if user_id in OWNER_IDS:
        btns.append([InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")])
    return InlineKeyboardMarkup(btns)

async def stop_project_process(project_id):
    """پروسیس کو میموری اور بیک گراؤنڈ سے روکنے کے لیے"""
    if project_id in ACTIVE_PROCESSES:
        proc = ACTIVE_PROCESSES[project_id]
        try:
            proc.terminate()
            # تھوڑا انتظار کریں اگر بند نہ ہو تو زبردستی مار دیں
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        except Exception as e:
            logging.error(f"Error killing process: {e}")
        del ACTIVE_PROCESSES[project_id]

# ================= START & AUTH FLOW =================

@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    user_id = message.from_user.id
    
    if await is_authorized(user_id):
        await message.reply_text(
            f"👋 Welcome back, **{message.from_user.first_name}**!\n\nMaster Bot Panel میں خوش آمدید۔ نیچے دیے گئے مینیو سے آپشن منتخب کریں۔",
            reply_markup=get_main_menu(user_id)
        )
    else:
        # اگر ٹوکن دیا گیا ہو: /start TOKEN_HERE
        if len(message.command) > 1:
            token = message.command[1]
            key_doc = await keys_col.find_one({"key": token, "status": "active"})
            
            if key_doc:
                await keys_col.update_one({"_id": key_doc["_id"]}, {"$set": {"status": "used", "used_by": user_id}})
                await users_col.insert_one({"user_id": user_id, "joined_at": message.date})
                await message.reply_text("✅ **Access Granted!** آپ کا ٹوکن ویریفائی ہو گیا ہے۔", reply_markup=get_main_menu(user_id))
            else:
                await message.reply_text("❌ **Invalid or Used Token.** براہ کرم ایڈمن سے درست ٹوکن لیں۔")
        else:
            await message.reply_text(
                "🔒 **Access Denied**\n\nیہ بوٹ پرائیویٹ ہے۔ استعمال کرنے کے لیے آپ کے پاس **Access Key** ہونی چاہیے۔\n\nاستعمال کا طریقہ:\n`/start <your_access_key>`"
            )

# ================= OWNER PANEL =================

@app.on_callback_query(filters.regex("owner_panel"))
async def owner_panel_cb(client, callback):
    if callback.from_user.id not in OWNER_IDS:
        return await callback.answer("Only for Owners!", show_alert=True)
    
    btns = [
        [InlineKeyboardButton("🔑 Generate New Key", callback_data="gen_key")],
        [InlineKeyboardButton("📜 List Active Keys", callback_data="list_keys")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    await callback.message.edit_text("👑 **Owner Panel**\n\nیہاں سے آپ ایکسیس کنٹرول کر سکتے ہیں۔", reply_markup=InlineKeyboardMarkup(btns))

@app.on_callback_query(filters.regex("gen_key"))
async def generate_key(client, callback):
    new_key = str(uuid.uuid4())[:8] # چھوٹی کی (Key) جنریٹ کریں
    await keys_col.insert_one({"key": new_key, "status": "active", "created_by": callback.from_user.id})
    
    text = f"✅ **New Access Key Created:**\n\n`{new_key}`\n\nیوزر کو یہ کمانڈ دیں:\n`/start {new_key}`"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="owner_panel")]]))

# ================= DEPLOYMENT FLOW =================

@app.on_callback_query(filters.regex("deploy_new"))
async def deploy_start(client, callback):
    user_id = callback.from_user.id
    USER_STATE[user_id] = {"step": "ask_name"}
    await callback.message.edit_text("📂 **New Project**\n\nاپنے پروجیکٹ کا کوئی نام لکھ کر بھیجیں (English only, no spaces).\nمثال: `mybot1`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="main_menu")]]))

@app.on_message(filters.text & filters.private)
async def handle_text_input(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE:
        state = USER_STATE[user_id]
        
        # Step 1: Project Name
        if state["step"] == "ask_name":
            proj_name = message.text.strip().replace(" ", "_")
            
            # Check if name already exists for this user
            exist = await projects_col.find_one({"user_id": user_id, "name": proj_name})
            if exist:
                return await message.reply("❌ اس نام سے پہلے ہی ایک پروجیکٹ موجود ہے۔ کوئی اور نام لکھیں۔")
            
            USER_STATE[user_id] = {"step": "wait_files", "name": proj_name, "files": {}}
            await message.reply(f"✅ پروجیکٹ کا نام: **{proj_name}**\n\nاب مجھے دو فائلیں بھیجیں:\n1. `requirements.txt`\n2. `main.py` (یا آپ کی مین فائل)\n\n(ایک ایک کر کے بھیجیں)")

@app.on_message(filters.document & filters.private)
async def handle_file_upload(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE and USER_STATE[user_id]["step"] == "wait_files":
        file_name = message.document.file_name
        proj_data = USER_STATE[user_id]
        
        # فولڈر بنائیں
        base_path = f"./deployments/{user_id}/{proj_data['name']}"
        os.makedirs(base_path, exist_ok=True)
        
        # فائل ڈاؤنلوڈ کریں
        await message.download(file_name=os.path.join(base_path, file_name))
        
        if file_name == "requirements.txt":
            proj_data["files"]["req"] = True
            await message.reply("📄 Requirements فائل مل گئی۔")
        elif file_name.endswith(".py"):
            proj_data["files"]["main"] = file_name
            await message.reply(f"🐍 Python فائل ({file_name}) مل گئی۔")
        
        # چیک کریں اگر دونوں فائلیں آ گئی ہیں
        if "req" in proj_data["files"] and "main" in proj_data["files"]:
            del USER_STATE[user_id] # اسٹیٹ ختم
            await start_deployment(client, message.chat.id, user_id, proj_data["name"], proj_data["files"]["main"])

async def start_deployment(client, chat_id, user_id, proj_name, main_file):
    msg = await client.send_message(chat_id, f"⚙️ **Deploying {proj_name}...**\nLibrarires install ہو رہی ہیں، انتظار کریں...")
    
    base_path = f"./deployments/{user_id}/{proj_name}"
    
    # 1. Install Requirements
    # --target کا استعمال تاکہ گلوبل خراب نہ ہو، لیکن سادگی کے لیے ہم venv کے بغیر کر رہے ہیں کیونکہ کنٹینر الگ ہے۔
    install_cmd = f"pip install -r {base_path}/requirements.txt"
    proc = await asyncio.create_subprocess_shell(
        install_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        # اگر انسٹالیشن فیل ہو جائے
        with open(f"{base_path}/install_error.txt", "w") as f:
            f.write(stderr.decode())
        await msg.delete()
        await client.send_document(chat_id, f"{base_path}/install_error.txt", caption=f"❌ **Installation Failed** for {proj_name}. Log file check karein.")
        return

    # 2. Run Python Script
    await msg.edit_text("🚀 **Starting Bot Script...**")
    
    # لاگ فائل کھولیں
    log_file = open(f"{base_path}/runtime_error.log", "w")
    
    # بوٹ چلائیں
    # cwd (Current Working Directory) سیٹ کرنا ضروری ہے تاکہ وہ اسی فولڈر میں سمجھے
    run_proc = await asyncio.create_subprocess_exec(
        "python3", main_file,
        cwd=base_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=log_file # ایرر فائل میں جائے گا
    )
    
    # ڈیٹا بیس اور میموری میں محفوظ کریں
    project_id = f"{user_id}_{proj_name}"
    ACTIVE_PROCESSES[project_id] = run_proc
    
    await projects_col.update_one(
        {"user_id": user_id, "name": proj_name},
        {"$set": {"status": "Running", "main_file": main_file, "path": base_path}},
        upsert=True
    )
    
    await msg.edit_text(f"✅ **{proj_name} Deployed Successfully!**\n\nاب یہ بیک گراؤنڈ میں چل رہا ہے۔")
    
    # 3. Monitor for Early Crash (5 seconds check)
    await asyncio.sleep(5)
    if run_proc.returncode is not None:
        # مطلب بوٹ فوراً بند ہو گیا
        log_file.close()
        await client.send_document(chat_id, f"{base_path}/runtime_error.log", caption=f"⚠️ **{proj_name} Crashed!**\nبوٹ سٹارٹ ہوا لیکن فوراً بند ہو گیا۔ ایرر لاگ چیک کریں۔")
        del ACTIVE_PROCESSES[project_id]
        await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"status": "Crashed"}})

# ================= MANAGEMENT FLOW =================

@app.on_callback_query(filters.regex("manage_projects"))
async def list_projects(client, callback):
    user_id = callback.from_user.id
    projects = projects_col.find({"user_id": user_id})
    
    btns = []
    async for p in projects:
        status = "🟢" if p.get("status") == "Running" else "🔴"
        btns.append([InlineKeyboardButton(f"{status} {p['name']}", callback_data=f"p_menu_{p['name']}")])
    
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await callback.message.edit_text("📂 **Your Projects**\nمینج کرنے کے لیے کلک کریں:", reply_markup=InlineKeyboardMarkup(btns))

@app.on_callback_query(filters.regex(r"^p_menu_"))
async def project_menu(client, callback):
    proj_name = callback.data.split("_")[2]
    user_id = callback.from_user.id
    
    btns = [
        [InlineKeyboardButton("🛑 Stop", callback_data=f"act_stop_{proj_name}"), InlineKeyboardButton("▶️ Start", callback_data=f"act_start_{proj_name}")],
        [InlineKeyboardButton("♻️ Restart", callback_data=f"act_restart_{proj_name}")],
        [InlineKeyboardButton("📤 Update File", callback_data=f"act_update_{proj_name}")],
        [InlineKeyboardButton("🗑️ Delete", callback_data=f"act_delete_{proj_name}")],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_projects")]
    ]
    await callback.message.edit_text(f"⚙️ **Managing: {proj_name}**\nکیا کرنا چاہتے ہیں؟", reply_markup=InlineKeyboardMarkup(btns))

@app.on_callback_query(filters.regex(r"^act_"))
async def project_actions(client, callback):
    action, proj_name = callback.data.split("_")[1], callback.data.split("_")[2]
    user_id = callback.from_user.id
    proj_id = f"{user_id}_{proj_name}"
    
    doc = await projects_col.find_one({"user_id": user_id, "name": proj_name})
    if not doc:
        return await callback.answer("Project not found!", show_alert=True)
        
    if action == "stop":
        await stop_project_process(proj_id)
        await projects_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "Stopped"}})
        await callback.answer("Project Stopped.")
        await list_projects(client, callback) # Refresh List
        
    elif action == "start":
        await callback.message.edit_text("Starting...")
        await start_deployment(client, callback.message.chat.id, user_id, proj_name, doc["main_file"])
        
    elif action == "restart":
        await stop_project_process(proj_id)
        await callback.message.edit_text("Restarting...")
        await start_deployment(client, callback.message.chat.id, user_id, proj_name, doc["main_file"])
        
    elif action == "update":
        # Ask user which file
        btns = [
            [InlineKeyboardButton("🐍 Update Python File", callback_data=f"upd_py_{proj_name}")],
            [InlineKeyboardButton("📄 Update Requirements", callback_data=f"upd_req_{proj_name}")]
        ]
        await callback.message.edit_text("کون سی فائل اپڈیٹ کرنی ہے؟", reply_markup=InlineKeyboardMarkup(btns))

    elif action == "delete":
        await stop_project_process(proj_id)
        await projects_col.delete_one({"_id": doc["_id"]})
        shutil.rmtree(doc["path"], ignore_errors=True) # فولڈر ڈیلیٹ
        await callback.answer("Project Deleted!")
        await list_projects(client, callback)

# Update Logic Handling
@app.on_callback_query(filters.regex(r"^upd_"))
async def ask_update_file(client, callback):
    type_, proj_name = callback.data.split("_")[1], callback.data.split("_")[2]
    user_id = callback.from_user.id
    
    USER_STATE[user_id] = {"step": "update_file", "name": proj_name, "type": type_}
    await callback.message.edit_text(f"📤 **Upload New File**\n\nبرائے مہربانی نئی فائل بھیجیں ({type_}).")

@app.on_message(filters.document & filters.private)
async def handle_update_upload(client, message):
    user_id = message.from_user.id
    # اگر سٹیٹ اپڈیٹ والی ہے تو یہاں کیچ ہوگا
    if user_id in USER_STATE and USER_STATE[user_id]["step"] == "update_file":
        data = USER_STATE[user_id]
        proj_name = data["name"]
        
        base_path = f"./deployments/{user_id}/{proj_name}"
        file_name = message.document.file_name
        
        # پرانی فائل ڈیلیٹ کر کے نئی رکھیں
        # نوٹ: ہم نام وہی رکھیں گے جو پروجیکٹ کا اصل تھا تاکہ کنفیوژن نہ ہو، یا جو یوزر نے بھیجا
        save_path = os.path.join(base_path, file_name)
        
        await message.download(save_path)
        
        await message.reply("✅ **File Updated!**\nاب بوٹ دوبارہ اسٹارٹ ہو رہا ہے...")
        
        # اگر python فائل تھی تو DB میں مین فائل کا نام بھی اپڈیٹ کر دیں اگر چینج ہوا ہے
        if data["type"] == "py":
            await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"main_file": file_name}})
        
        # ری اسٹارٹ لاجک
        proj_id = f"{user_id}_{proj_name}"
        await stop_project_process(proj_id) # پرانا روکیں
        
        doc = await projects_col.find_one({"user_id": user_id, "name": proj_name})
        await start_deployment(client, message.chat.id, user_id, proj_name, doc["main_file"]) # نیا چلائیں
        
        del USER_STATE[user_id]

# ================= GENERAL NAVIGATION =================
@app.on_callback_query(filters.regex("main_menu"))
async def back_main(client, callback):
    await callback.message.edit_text("🏠 **Main Menu**", reply_markup=get_main_menu(callback.from_user.id))

if __name__ == "__main__":
    print("Master Bot Started...")
    app.run()
