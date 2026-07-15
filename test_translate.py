import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from deep_translator import GoogleTranslator, MyMemoryTranslator

print("Testing GoogleTranslator...")
try:
    print(GoogleTranslator(source='auto', target='vi').translate('Hello world'))
except Exception as e:
    print('GoogleTranslator Error:', type(e), e)

print("Testing MyMemoryTranslator...")
try:
    print(MyMemoryTranslator(source='en', target='vi').translate('Hello world'))
except Exception as e:
    print('MyMemory Error:', type(e), e)
