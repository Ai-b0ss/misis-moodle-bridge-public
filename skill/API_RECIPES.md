# Moodle MISIS — проверенные рецепты API

**Обновлено 23.10.2026.** Документ дополняет `SKILL.md` и предназначен для выполнения на авторизованной Windows-машине, а не в облаке. По умолчанию только чтение. Не копировать Moodle-токен, DPAPI-секрет или полный объект `site_info` в чат, файлы, журналы и инструменты других доменов.

## Запуск временных проверок

В каталоге `C:\Users\user\Documents\MISIS-Moodle-Bridge` доступен `moodle_client.py`. Сначала выполнить `check`. Дальше возможен Python-скрипт, расположенный в той же директории или работающий с ней как текущей. Удобный общий каркас:

```python
import html
import re
import moodle_client as m

token, site = m.site_info(m.read_credential())  # не печатать!
user_id = site['userid']

def api(name, params=None):
    response = m.call(token, name, params or {})
    if isinstance(response, dict) and response.get('warnings'):
        print(name, 'warnings:', len(response['warnings']))
    return response
```

Если `courseids` — массив, заполняй плоские поля `courseids[0]`, `courseids[1]` и т.д. для `m.call`; Python-список `{'courseids': [id]}` с текущим клиентом не работает. Никогда не выводи `token`, `site` или сырые URL с авторизацией.

## 1. Курсы, разделы, типы модулей

```python
courses = api('core_enrol_get_users_courses', {'userid': user_id})
for course in courses:
    print(course['id'], course['fullname'])

course_id = next(c['id'] for c in courses if 'Введение в специальность' in c['fullname'])
sections = api('core_course_get_contents', {'courseid': course_id})
for section in sections:
    print('SECTION:', section.get('name'))
    for mod in section.get('modules', []):
        print(mod.get('id'), mod.get('instance'), mod.get('modname'), mod.get('name'), mod.get('url'))
```

`mod['id']` — course module ID (`cmid`), `mod['instance']` — ID экземпляра модуля (если выдан); `lessonid`, `assignid`, `quizid` — не взаимозаменяемые значения. По `cmid` можно читать `core_course_get_course_module({'cmid': cmid})`. Вложенные файлы ресурса часто перечислены в `mod['contents']` с `filename`, `fileurl`, `mimetype`.

## 2. HTML-страницы и встроенные Google Slides

```python
pages = api('mod_page_get_pages_by_courses', {'courseids[0]': course_id})['pages']
for page in pages:
    source = html.unescape(page.get('content') or '')
    links = re.findall(r'''(?:src|href)\s*=\s*["']([^"']+)["']''', source, re.I)
    print(page.get('coursemodule'), page.get('name'), 'links:', links[:20])
```

Результат содержит опубликованные `iframe` и `<a>` на Google Slides, PDF, медиа или внешние ресурсы. Не считать все ссылки скачиваемыми файлами и не утверждать, что конкретный слайд прочитан, пока не получен и сверён его текст или изображение.

## 3. Интерактивный `lesson`: порядок вызовов

Обычный запрос страницы по `mod_lesson_get_page_data` может завершиться `cannotfindtimer`. Это не означает, что страницу нельзя открыть. Однако вызов `mod_lesson_launch_attempt` **изменяет состояние**: открывает или продолжает попытку. Перед ним проверить условия урока и разрешение пользователя.

```python
lessons = api('mod_lesson_get_lessons_by_courses', {'courseids[0]': course_id})['lessons']
for l in lessons:
    print(l['id'], l['coursemodule'], l['name'])

lesson_id = 5338          # пример только для курса ВВС-2026, сначала вычислить заново
lesson = api('mod_lesson_get_lesson', {'lessonid': lesson_id})['lesson']
access = api('mod_lesson_get_lesson_access_information', {'lessonid': lesson_id})
meta = api('mod_lesson_get_pages', {'lessonid': lesson_id})['pages']
print('practice=', lesson['practice'], 'maxattempts=', lesson['maxattempts'])
print('access_reasons=', access.get('preventaccessreasons', []))
page_id = access['firstpageid']   # или meta[0]['page']['id']

# СНАЧАЛА только чтение; при отсутствии таймера сервер может вернуть cannotfindtimer:
# page = api('mod_lesson_get_page_data', {'lessonid': lesson_id, 'pageid': page_id})

# ТОЛЬКО ПОСЛЕ ЗАПРОСА ПОЛЬЗОВАТЕЛЯ И ПРОВЕРКИ РИСКА:
# api('mod_lesson_launch_attempt', {'lessonid': lesson_id})
# page = api('mod_lesson_get_page_data', {'lessonid': lesson_id, 'pageid': page_id})
# source = html.unescape(page['page'].get('contents', ''))
# print(re.findall(r'<iframe[^>]+src=["\']([^"\']+)', source, re.I))
```

Проверено 23 октября 2026: на третьей лекции ВВС `lessonid=5338`, `cmid=137044`, `firstpageid=25554` после запуска тренировочной попытки вернулся `page.title='Презентация'` и Google Slides `iframe` в **`page['contents']`**. Урок №2: `lessonid=5337`, №4: `lessonid=5339`; ID актуальны только для данной версии курса.

Запрещено автоматически вызывать `mod_lesson_process_page`, `mod_lesson_finish_attempt` и `mod_lesson_view_lesson`. Никаких ответов, фиксации завершения и расходования оцениваемой попытки ради извлечения URL. Оценивать `practice`, `grade`, `maxattempts`, `timelimit`, `retake`, фактический статус, возможность посмотреть без запуска. Наличие `practice=True` снижает некоторые риски, но не отменяет фиксацию открытия.

## 4. Просмотр опубликованной презентации

Из полученного `iframe` брать **точный** URL, декодировать `&amp;`, проверить заголовок и структуру. Для просмотра допустимо использовать `Remote_Desktop_Commander.read_file(path=URL, isUrl=true)` — проверено на опубликованном просмотрщике третьей лекции. Сработало извлечение HTML и имени `Ввс_ББИ_23_Лекция_3.pptx` из метаданных. Содержимое HTML может включать текст слайдов в `aria-label` и других структурах, но наличие подстроки не обеспечивает полноты.

Практический порядок анализа:

1. Убедиться, что URL действительно опубликован автором и открывается без Moodle-токена.
2. Получить HTML, найти мета-заголовок, текстовые признаки слайдов и заголовки; делать поиск без учёта регистра, с HTML-декодированием.
3. Попробовать допустимый для публикации просмотр/экспорт PDF (если он доступен в данной презентации); не выдумывать скачивание, если сервер отказывает.
4. При наличии PDF/PPTX проверить реальные страницы/слайды, текст, схемы и изображения; сложную графику смотреть визуально.
5. Указывать номер слайда только по надёжно установленному соответствию. При частичном покрытии явно говорить об этом.

Google Slides, прикреплённые к уроку, могут быть размещены у Google публично либо иметь собственные права. `pubembed` не следует автоматически преобразовывать в `edit`: это может быть другой URL/право. Токены Moodle в Google не передавать.

## 5. Задания, отправки, замечания

```python
bundle = api('mod_assign_get_assignments', {'courseids[0]': course_id})
for c in bundle.get('courses', []):
    for assignment in c.get('assignments', []):
        print(assignment['id'], assignment.get('name'), assignment.get('duedate'))
        # Для точечного разбора отправки:
        # detail = api('mod_assign_get_submission_status', {'assignid': assignment['id']})
        # print(detail.keys())  # сначала изучить структуру без печати секретов
```

Сверять: `duedate` и `cutoffdate`, предоставленные преподавателем материалы и вложения, submission status, отметку окончательной сдачи, обратную связь, комментарии, файлы и выставленную оценку. Сроки Moodle хранятся как Unix timestamp — переводить в часовой пояс пользователя/курса, не сравнивать вручную текст дат. Значение `new` и отсутствие completion не являются доказательством несданности; учесть тип модуля и реальные записи о submission.

Для ответов, требующих работы с файлом: скачать через разрешённый Moodle API на Windows, проверить байтовую сигнатуру, размер, формат, содержимое. Уже подготовленные локально черновики не отправлять, пока пользователь явно не попросит. Про отправку в `user/draft` см. основной SKILL.

## 6. Оценки, календарь, тесты и завершённость

```python
# Подробная таблица оценок, успешно проверено 23.10.2026:
grade_table = api('gradereport_user_get_grades_table', {'courseid': course_id, 'userid': user_id})

# Статусы элементов курса (отличать «не настроено» от «не выполнено»):
completion = api('core_completion_get_activities_completion_status',
                 {'courseid': course_id, 'userid': user_id})

# Ближайшие события, сроки, активность:
calendar = api('core_calendar_get_calendar_upcoming_view')

# Существующие тесты:
quizzes = api('mod_quiz_get_quizzes_by_courses', {'courseids[0]': course_id})
# Далее при необходимости — mod_quiz_get_user_attempts, без start_attempt.
```

Другие безопасные методы, уже подтверждённые в предыдущих проверках: `gradereport_user_get_grade_items`, `gradereport_overview_get_course_grades`, `mod_assign_get_submission_status`, `message_popup_get_popup_notifications`, `core_message_get_unread_notification_count`, `core_message_get_conversations`, `mod_quiz_get_user_attempts`, `mod_forum_get_forums_by_courses`, `mod_forum_get_forum_discussions`, `core_calendar_get_action_events_by_timesort`.

**Уведомления:** не вызывать методы mark-read, даже если кажется, что они относятся к чтению. **Тесты:** не запускать новые попытки, не сохранять/обрабатывать ответы. **Completion:** не менять ручные отметки.

## 7. Соседние типы материалов

Встречаются `resource`, `folder`, `book`, `page`, `lesson`, `assign`, `quiz`, `forum`. В текущем Moodle подтверждены следующие **работающие вызовы** (в исследованном курсе ВВС разделы resource/folder/book были пустыми):

```python
api('mod_resource_get_resources_by_courses', {'courseids[0]': course_id})
api('mod_folder_get_folders_by_courses', {'courseids[0]': course_id})
api('mod_book_get_books_by_courses', {'courseids[0]': course_id})
api('core_course_get_course_module', {'cmid': 137044})
```

При поиске содержания сначала инвентаризовать ВСЕ типы модулей курса, а не только загрузки файлов. Для папок и ресурсов проверять данные `contents` (имя, MIME, размер и `fileurl`). Для `book` проверять доступ к главам, отдельным форматам/вложениям и встроенным ссылкам в пределах студенческих прав. Не вызывать `*_view_*`, если они фиксируют просмотр.

## 8. Установленные ограничения и реакции

| Симптом | Обоснованное действие |
|---|---|
| `invalidparameter` | Проверить `courseids[0]`, `lessonid` vs `cmid`, структуру запроса; НЕ считать запретом автоматически |
| `cannotfindtimer` | Проверить статус урока; для просмотра может потребоваться **запуск попытки**, который не read-only |
| `globalsearchdisabled` | Использовать `courses` → фильтр имен → `core_course_get_contents` → HTML/вложения; не обещать глобальный поиск |
| Пустой `core_course_search_courses` | Метод может работать, но не обнаружить enrolled-курс; использовать явное перечисление `core_enrol_get_users_courses` |
| `No access rights in module context` в `warnings` | Зафиксировать частичный охват, проверить допустимые соседние источники и не обходить скрытые материалы |
| Ссылка на презентацию есть, но HTML не парсится | Открыть опубликованный просмотрщик визуально или запросить доступный PDF; не выдавать HTML за проверенные слайды |
| Курс/файл/ПК недоступен | Проверить соединение, фактические полномочия, статус ресурса, потом честно обозначить ограничение |

## 9. Уровни подтверждения и контроль безопасности

Описывать результаты точно:

- **API доступен:** получен успешный ответ функции.
- **Ресурс найден:** известен `cmid`, тип, URL.
- **Ресурс открыт:** получена полная страница, файл или публичный документ.
- **Содержание проверено:** разобраны требуемые текст/страницы/слайды, при необходимости просмотрено изображение.
- **Действие выполнено:** есть подтверждение Moodle от write-вызова; не заявлять без него.

Выполнение любого запроса должно сохранять приватность и текущий статус сдачи работ. Для каждой проверки заранее знать, **читает** метод или **изменяет** состояние. Для учебного урока `mod_lesson_launch_attempt` официально относится к write, несмотря на то что не отправляет ответ. Метод `mod_lesson_view_lesson` — тоже write. При непроверенной функции сначала сверяться с документацией и доступностью API сервера.

Источники API:
- https://docs.moodle.org/dev/index.php?title=Web_service_API_functions
- https://docs.moodle.org/dev/Web_services
- https://phpdoc.moodledev.io/4.5/d4/d0c/classmod__lesson__external.html
