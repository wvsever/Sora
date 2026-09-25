add_executable(sora_tests test_main.cpp test_core.cpp test_projection.cpp test_collateral.cpp)
target_link_libraries(sora_tests PRIVATE sora_core)
target_include_directories(sora_tests PRIVATE ${PROJECT_SOURCE_DIR}/third_party/doctest)
add_test(NAME sora_unit COMMAND sora_tests)

# End-to-end: run the engine on the reference SIM and compare with the golden results.
find_package(Python3 COMPONENTS Interpreter)
if(Python3_FOUND)
  add_test(NAME sora_golden
    COMMAND ${Python3_EXECUTABLE} ${PROJECT_SOURCE_DIR}/tools/compare_golden.py
      --engine $<TARGET_FILE:sora> --golden ${PROJECT_SOURCE_DIR}/tests/golden/20260630
      --scenario ${PROJECT_SOURCE_DIR}/tests/scenarios/test_eba2025.yaml
    WORKING_DIRECTORY ${PROJECT_SOURCE_DIR})
endif()
