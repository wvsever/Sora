# REST client for the regulatory calculator (src/calculator.cpp), IRB REA (src/rea.cpp), credit parameters
# (src/credit_parameters.cpp) and their tests.
# Included at the end of the top-level CMakeLists.txt, after sora_core and sora_tests exist.
#
# Vendored single headers (SYSTEM, so their warnings stay out of the build):
#   third_party/httplib/httplib.h  cpp-httplib 0.18.1
#   third_party/nlohmann/json.hpp  nlohmann/json 3.11.3
# TLS: OpenSSL (CPPHTTPLIB_OPENSSL_SUPPORT) when found; SORA_CALCULATOR_TLS=OFF builds a plain-HTTP client.

option(SORA_CALCULATOR_TLS "Build the calculator client with TLS (OpenSSL)" ON)

target_sources(sora_core PRIVATE ${PROJECT_SOURCE_DIR}/src/calculator.cpp ${PROJECT_SOURCE_DIR}/src/rea.cpp
  ${PROJECT_SOURCE_DIR}/src/credit_parameters.cpp)
target_include_directories(sora_core SYSTEM PRIVATE
  ${PROJECT_SOURCE_DIR}/third_party/httplib ${PROJECT_SOURCE_DIR}/third_party/nlohmann)
find_package(Threads REQUIRED)
target_link_libraries(sora_core PRIVATE Threads::Threads)

if(SORA_CALCULATOR_TLS)
  find_package(OpenSSL 3.0)
  if(OpenSSL_FOUND)
    target_compile_definitions(sora_core PRIVATE CPPHTTPLIB_OPENSSL_SUPPORT)
    target_link_libraries(sora_core PRIVATE OpenSSL::SSL OpenSSL::Crypto)
  else()
    message(WARNING "OpenSSL not found: the calculator client supports http:// only")
  endif()
endif()

if(SORA_BUILD_TESTS AND TARGET sora_tests)
  target_sources(sora_tests PRIVATE ${PROJECT_SOURCE_DIR}/tests/cpp/test_calculator.cpp)
  target_include_directories(sora_tests SYSTEM PRIVATE ${PROJECT_SOURCE_DIR}/third_party/nlohmann)

  # Engine + stub calculator end to end (python/tests/test_calculator_engine.py, IRB REA, and
  # test_calculator_parameters_engine.py, credit parameters; skip without the test data).
  find_package(Python3 COMPONENTS Interpreter)
  if(Python3_FOUND)
    add_test(NAME sora_calculator
      COMMAND ${Python3_EXECUTABLE} -m pytest -q -p no:cacheprovider tests/test_calculator_engine.py
        tests/test_calculator_parameters_engine.py
      WORKING_DIRECTORY ${PROJECT_SOURCE_DIR}/python)
    set_tests_properties(sora_calculator PROPERTIES ENVIRONMENT "SORA_ENGINE=$<TARGET_FILE:sora>" TIMEOUT 1200)
  endif()
endif()
