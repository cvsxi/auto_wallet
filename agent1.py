import google.generativeai as genai
import api_config
import openclaw

# Правильне налаштування: вказуємо іменований параметр api_key
genai.configure(api_key=api_config.GEMINI_API_KEY)
def start_gemini_chat():
    # Спробуємо використати найстабільнішу назву моделі
    model_name = 'gemini-2.5-flash' 
    
    try:
        # Створення моделі з системною інструкцією
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction="Ти — корисний асистент. Відповідай українською мовою."
        )
        
        # Початок чату
        chat = model.start_chat(history=[])
        
        print(f"--- Чат-бот ({model_name}) запущений! ---")
        print("(Напишіть 'exit', щоб вийти)")

        while True:
            user_text = input("Ви: ")
            if user_text.lower() in ['exit', 'вихід', 'quit']:
                break
            
            response = chat.send_message(user_text)
            print(f"Бот: {response.text}")
            
    except Exception as e:
        print(f"Сталася помилка: {e}")
        print("\nСпробуємо знайти доступні моделі для вашого ключа...")
        # Виводимо список доступних моделей, якщо виникла помилка
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                print(f"- {m.name}")

# Example usage of openclaw (replace with actual implementation as needed)
def use_openclaw():
    print("Using openclaw library...")
    # Add your openclaw-specific logic here

if __name__ == "__main__":
    use_openclaw()
    start_gemini_chat()