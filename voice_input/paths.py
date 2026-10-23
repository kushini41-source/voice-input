"""所有檔案路徑集中在這裡，一律以程式所在資料夾為基準（資料夾名稱「语音输入」不要改）。"""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
DICTIONARY_PATH = os.path.join(BASE_DIR, "dictionary.yaml")
API_KEY_PATH = os.path.join(BASE_DIR, "api_key.txt")
CUDA_LIBS_DIR = os.path.join(BASE_DIR, "cuda_libs")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "voice_input.log")
HISTORY_DIR = os.path.join(BASE_DIR, "history")
