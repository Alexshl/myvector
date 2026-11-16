"""Writes golden.jsonl (hand-authored by an AI after reading the code) and validates that all expected files exist in git."""
import json, os, subprocess
HOME = os.path.expanduser("~/projects")
S, M, O, R, F = "project-b/site", "project-b/maximus-libs", "project-b/ocr", "project-d.se/public_html", "project-g"
def e(repo, *paths): return [f"{repo}/{p}" for p in paths]
G = [
 # ---- project-b/site
 ("Як адміністратор заходить у систему від імені іншого користувача і чому при цьому скидається підтвердження пароля?", "uk", "semantic", e(S,"app/Actions/Admin/Users/ImpersonateUser.php")),
 ("Как закрывается бухгалтерский период до конца месяца так, чтобы повторное закрытие не создавало дубликатов?", "ru", "semantic", e(S,"app/Actions/Closing/LockPeriod.php")),
 ("Pair a card refund with the original purchase made within the previous two months for the same amount", "en", "semantic", e(S,"app/Actions/Bank/PairCardRefunds.php")),
 ("Скасування автоматично проведеного банківського платежу з поверненням рядка у стан пропозиції", "uk", "semantic", e(S,"app/Actions/Bank/UndoAutoRegistration.php")),
 ("Which VAT rate is applied to a subscription invoice depending on the customer's country and a valid EU VAT number check?", "en", "semantic", e(S,"app/Billing/Tax/VatCalculator.php")),
 ("Какой шаг напоминания о неоплаченном счёте выбирается в зависимости от числа дней просрочки?", "ru", "semantic", e(S,"app/Support/Reminders/ReminderStepResolver.php")),
 ("Перевірка контрольної суми міжнародного банківського рахунку", "uk", "semantic", e(S,"app/Support/Iban.php")),
 ("Constant-time comparison of webhook signatures with a timestamp tolerance to stop replay attacks", "en", "semantic", e(S,"app/Integrations/Webhooks/HmacSignature.php")),
 ("Подсчёт итогов по строкам коммерческого предложения с группировкой НДС по ставкам", "ru", "semantic", e(S,"app/Support/Quotes/QuoteTotals.php")),
 ("Запит на видалення компанії: що змінюється в базі даних і кого повідомляють", "uk", "semantic", e(S,"app/Actions/Companies/RequestDeletion.php")),
 ("Block two parallel changes of the same company's external database logins using a database lock", "en", "semantic", e(S,"app/Actions/Admin/Companies/DbAccess/DbAccessGuard.php")),
 # ---- maximus-libs
 ("Back off and pause status polling when the government server asks us to wait before the next request", "en", "semantic", e(M,"native/Common/EdsHttp.cs")),
 ("Генерація QR-коду з кодами Ріда-Соломона для виправлення помилок", "uk", "semantic", e(M,"native/Project-BOcr/QrCode.cs")),
 ("Определение того, есть ли в PDF пригодный текстовый слой, и обнаружение текста с разрядкой между буквами", "ru", "semantic", e(M,"native/Project-BRecognize/PdfTextLayer.cs")),
 ("Перевірка згенерованого XML електронного рахунку на відповідність правилам UBL до відправки", "uk", "semantic", e(M,"native/Project-BEInvoice/UblChecker.cs")),
 # ---- project-b/ocr
 ("How are API keys generated so that only a hash is stored and the plaintext is shown just once?", "en", "semantic", e(O,"functions/ocr/core/security.py","functions/ocr/services/keys.py")),
 ("Розбір сум, записаних з пробілами та комами як роздільниками тисяч і десяткових знаків", "uk", "semantic", e(O,"functions/ocr/recognition/normalizer.py")),
 ("Как помечаются неудачными документы, слишком долго зависшие в обработке, и когда с клиента списываются деньги?", "ru", "semantic", e(O,"functions/ocr/services/maintenance.py")),
 ("Доставка вебхуків з кількома повторними спробами та перевіркою дозволеної адреси", "uk", "semantic", e(O,"functions/ocr/services/webhooks.py")),
 ("Switch to another AI provider when the first recognition driver fails on a document", "en", "semantic", e(O,"functions/ocr/recognition/strategy.py")),
 # ---- project-d
 ("Normalise a Swedish telephone number written in any format down to its last nine digits", "en", "semantic", e(R,"api/caller-lookup/src/Phone.php")),
 ("Проверка совместимости выбранных параметров линз: у бифокальных доступен только индекс 1.5", "ru", "semantic", e(R,"api/configurator/src/CompatibilityRules.php")),
 ("Підписані посилання для завантаження файлів з обмеженим терміном дії", "uk", "semantic", e(R,"api/mcp/src/SignedLink.php")),
 ("Очистка HTML от опасных тегов скриптов и iframe в редакторском контенте", "ru", "semantic", e(R,"api/mcp/src/Sanitizer.php")),
 ("Запис у журнал аудиту старого та нового значення поля перед оновленням", "uk", "semantic", e(R,"api/mcp/src/Audit.php")),
 ("Synchronise lens component prices from the admin steps table with an insert-or-update statement", "en", "semantic", e(R,"api/configurator/src/PriceSyncer.php")),
 # ---- project-g
 ("Generate the next invoice number from a mask with year and month tokens and zero padding", "en", "semantic", e(F,"app/Actions/Orders/GenerateFakturaNumber.php")),
 ("Выбор рекомендуемого индекса линзы по оптической силе: если нужного нет в наличии, берётся ближайший более тонкий", "ru", "semantic", e(F,"app/Support/LensRules/LensIndexRecommendation.php")),
 ("Добовий бюджет на виклики LLM, що рахується від початку доби за стокгольмським часом", "uk", "semantic", e(F,"app/Ai/Intake/LlmBudget.php")),
 ("Автоматическое списание отложенного платежа Klarna, когда заказ переходит в статус отправлен", "ru", "semantic", e(F,"app/Actions/Orders/CaptureDinteroOnShipped.php")),
 ("Утримання адміністративного збору за коробку, що не повернулась, і скасування замовлень цієї коробки", "uk", "semantic", e(F,"app/Actions/Orders/KeepBoxFee.php")),
 ("Strip EXIF and GPS metadata and downscale a photo before it is sent to an external AI model", "en", "semantic", e(F,"app/Ai/Intake/ImageSanitizer.php")),
 ("Разбор ссылки на заказ из лаборатории: первые пять цифр это номер заказа, затем суффикс через дефис", "ru", "semantic", e(F,"app/Actions/Orders/MatchOmegaReference.php")),
 ("Стиснення фото рецепта у браузері перед завантаженням, щоб не перевищити ліміт розміру", "uk", "semantic", e(F,"resources/js/shop/composables/imageCompressor.ts")),
 ("How a percentage discount code is validated, applied as a negative order line and spent only after payment", "en", "semantic", e(F,"docs/discount-coupons.md")),
 # ---- identifiers
 ("needs_escalation", "en", "identifier", e(O,"functions/ocr/recognition/normalizer.py")),
 ("fail_stuck_documents", "en", "identifier", e(O,"functions/ocr/services/maintenance.py")),
 ("PairCardRefunds", "en", "identifier", e(S,"app/Actions/Bank/PairCardRefunds.php")),
 ("verifyTimestamped", "en", "identifier", e(S,"app/Integrations/Webhooks/HmacSignature.php")),
 ("IsLetterSpaced", "en", "identifier", e(M,"native/Project-BRecognize/PdfTextLayer.cs")),
 ("PausePoll", "en", "identifier", e(M,"native/Common/EdsHttp.cs")),
 ("PROJECT_D_MCP_SIGNING_KEY", "en", "identifier", e(R,"api/mcp/src/SignedLink.php")),
 ("mcp_audit_log", "en", "identifier", e(R,"api/mcp/src/Audit.php")),
 ("GenerateFakturaNumber", "en", "identifier", e(F,"app/Actions/Orders/GenerateFakturaNumber.php")),
 ("ai.intake.daily_budget_usd", "en", "identifier", e(F,"app/Ai/Intake/LlmBudget.php","config/ai.php")),
]
if __name__ == "__main__":
    repos = {r: f"{HOME}/{r}" for r in (S, M, O, R, F)}
    ok = True
    for q, lang, typ, exp in G:
        for x in exp:
            repo = next(r for r in repos if x.startswith(r + "/"))
            if not os.path.isfile(os.path.join(repos[repo], x[len(repo)+1:])):
                print("MISSING", x); ok = False
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden.jsonl"), "w") as fh:
        for q, lang, typ, exp in G:
            fh.write(json.dumps({"q": q, "lang": lang, "type": typ, "expected": exp}, ensure_ascii=False) + "\n")
    import collections
    print(len(G), collections.Counter((g[2], g[1]) for g in G), "OK" if ok else "ERRORS")
