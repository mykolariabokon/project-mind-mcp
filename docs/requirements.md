# Реєстр вимог ProjectMind MCP

Мінімальний реєстр, заведений з картки #1 (2026-10-19). Решта проєкту в реєстрі
ще не описана: це наступні партії, а не пропуск.

Доказ для всіх рядків виконується однією командою:
`.venvScriptspython.exe -m pytest tests -q -o addopts=`

| ID | Вимога | Предмет | Джерело | Ризик | Статус | Підтверджено | Картка | Заблоковано |
|----|--------|---------|---------|-------|--------|--------------|--------|-------------|
| R-001 | Кожен модуль верхнього рівня, який імпортує сервер, є в py-modules пакета | пакування | #1 | звичайний | done | поведінковий: tests/test_packaging_modules.py · test_every_imported_product_module_is_packaged @2026-10-19 | #1 | |
| R-002 | Граф символів бачить файл, створений після побудови, навіть глибоко у вкладеній теці | граф символів | #1 | звичайний | done | поведінковий: tests/test_symbol_graph_new_files.py · test_new_file_is_seen_without_index_metadata_at_depth @2026-10-19 | #1 | |
| R-003 | rank_bm25 і numpy імпортуються на старті в головному потоці, а не під час першого пошуку | пошук | #1 | звичайний | done | поведінковий: tests/test_cold_search.py · test_rank_bm25_is_imported_on_main_thread_by_warmup @2026-10-19 | #1 | |
| R-004 | Профіль lean лишає щонайбільше 20 інструментів, типовий full — усі 56 | інструменти | #1 | звичайний | done | поведінковий: tests/test_tool_profile.py · test_lean_registers_at_most_20_tools @2026-10-19 | #1 | |
| R-005 | Режим streamable-http відповідає на initialize на локальній адресі | транспорт | #1 | звичайний | done | поведінковий: tests/test_transport_http.py · test_streamable_http_initialize_round_trip @2026-10-19 | #1 | |
| R-006 | Теплий сервер бачить файл, створений після побудови графа: кеш графа в пам'яті перевіряється на застарілість | граф символів | #3 | звичайний | done | поведінковий: tests/test_symbol_graph_warm_cache.py · test_warm_cache_sees_file_created_after_build @2026-10-19 | #3 | |
| R-007 | Профіль lean не містить set_project_root: спільний теплий сервер не дає одному клієнтові змінити корінь проєкту для решти сесій | інструменти | #5 | доступ | done | поведінковий: tests/test_tool_profile.py · test_lean_has_no_set_project_root @2026-10-19 | #7 | |
| R-008 | У режимі streamable-http корінь проєкту зафіксований аргументом --project-root: set_project_root і session_init з чужим шляхом відмовляють без побічних ефектів, а друга сесія через окремий initialize бачить свій незмінений корінь | транспорт | #5, завдання 2026-10-20 п.1 | доступ | done | поведінковий: tests/test_http_root_lock.py · test_set_project_root_in_one_session_does_not_change_root_for_another @2026-10-20 | #8 | |
| R-009 | Кеш analyze_project_structure прив'язаний до кореня проєкту: після зміни кореня віддається структура нового проєкту, а не попереднього | транспорт | завдання 2026-10-20 п.1 (огляд спільного стану) | звичайний | done | поведінковий: tests/test_http_root_lock.py · test_structure_cache_does_not_leak_between_roots @2026-10-20 | #8 | |
| R-010 | Сервер міряє час імпортів, побудови й завантаження BM25, завантаження моделі та кожного виклику інструмента (INFO), а секція, що тримається довше за поріг, дає WARNING зі стеком потоку, що в ній стоїть | пошук | завдання 2026-10-20 п.3 | звичайний | done | поведінковий: tests/test_timing.py · test_a_stuck_section_logs_slow_with_the_stack_of_the_stuck_thread @2026-10-20 | #9 | |
| R-011 | Версія в pyproject.toml збігається з верхнім записом CHANGELOG, а колесо, зібране з чистої копії дерева, містить кожен модуль, який імпортує пакет, і точки входу projectmind-mcp та projectmind | пакування | завдання 2026-10-20 п.2 | звичайний | done | поведінковий: tests/test_release_metadata.py · test_wheel_contains_every_imported_product_module @2026-10-20 | #10 | |
