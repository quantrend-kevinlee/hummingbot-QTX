# Event Loops and Testing Framework Deep Dive

## Table of Contents
1. [Event Loops: The Foundation](#event-loops-the-foundation)
2. [The Testing Framework Problem](#the-testing-framework-problem)
3. [IsolatedAsyncioWrapperTestCase: The Solution](#isolatedasynciowrappertestcase-the-solution)
4. [Mock vs Patch: The Core Concepts](#mock-vs-patch-the-core-concepts)
5. [Comprehensive Testing Tools Reference](#comprehensive-testing-tools-reference)
6. [Complete Testing Flow](#complete-testing-flow)
7. [Best Practices and Common Pitfalls](#best-practices-and-common-pitfalls)
8. [Practical Examples](#practical-examples)

---

## Event Loops: The Foundation

### What is an Event Loop?

An **event loop** is a single-threaded cooperative multitasking system that manages the execution of asynchronous code:

```python
# Conceptual event loop structure
class EventLoop:
    def __init__(self):
        self.ready_queue = []     # Tasks ready to run
        self.waiting_tasks = {}   # Tasks waiting for I/O
        self.io_events = {}       # I/O event monitoring
        
    def run_until_complete(self, coro):
        # Convert coroutine to task
        task = Task(coro)
        self.ready_queue.append(task)
        
        # Main execution loop
        while self.ready_queue or self.waiting_tasks:
            # Execute ready tasks
            while self.ready_queue:
                task = self.ready_queue.pop(0)
                try:
                    # Run task until it yields (hits await)
                    next_step = task.step()
                    if task.done():
                        return task.result()
                    elif task.waiting_for_io():
                        self.waiting_tasks[task.id] = task
                except StopIteration as e:
                    return e.value
            
            # Poll I/O and move ready tasks back to queue
            self._poll_io_events()
            self._check_timeouts()
```

### Key Properties of Event Loops

1. **Single-threaded**: Only one coroutine executes at a time
2. **Cooperative**: Tasks yield control voluntarily at `await` points
3. **Non-blocking**: I/O operations don't block the entire loop
4. **Scheduling**: Manages when tasks run and resume
5. **Context**: Provides execution context for all async operations

### Async/Await Mechanics

```python
async def fetch_data():
    print("Starting fetch")
    await asyncio.sleep(1)  # Yields control back to event loop
    print("Fetch complete")  # Resumes here when sleep completes
    return "data"

# What actually happens step-by-step:
# 1. fetch_data() returns a coroutine object (not executed yet)
# 2. Event loop creates a Task wrapper around the coroutine
# 3. Task executes until first await (asyncio.sleep(1))
# 4. sleep(1) creates a timer and registers a callback
# 5. Task suspends and control returns to event loop
# 6. Event loop processes other tasks and handles I/O
# 7. After 1 second, timer callback resumes the task
# 8. Task continues execution from after the await
# 9. Task completes and returns the result
```

### Event Loop Lifecycle

```
┌─────────────────────────────────────────────────────────────────┐
│                    Event Loop Lifecycle                         │
└─────────────────────────────────────────────────────────────────┘

1. Creation
   ├── asyncio.new_event_loop()
   ├── Set loop as current thread's loop
   └── Initialize internal data structures

2. Task Scheduling
   ├── Tasks added to ready queue
   ├── I/O callbacks registered
   └── Timers scheduled

3. Execution Cycle
   ├── Run ready tasks until they yield
   ├── Poll I/O file descriptors
   ├── Process completed I/O operations
   ├── Execute timer callbacks
   └── Repeat until no more work

4. Cleanup
   ├── Cancel pending tasks
   ├── Close I/O resources
   ├── Run final callbacks
   └── Destroy loop
```

### Deep Dive: How Event Loops Work

#### The Core Execution Model

```python
# Simplified event loop execution cycle
def event_loop_cycle():
    while True:
        # Phase 1: Execute all ready tasks
        while ready_queue:
            task = ready_queue.popleft()
            try:
                # Run task until it hits 'await' (yields)
                result = task.send(None)
                if task.done():
                    task.set_result(result)
                else:
                    # Task yielded - waiting for something
                    if task.waiting_for_io():
                        io_waiting[task.io_fd] = task
                    elif task.waiting_for_timer():
                        timer_heap.push(task.wake_time, task)
            except StopIteration as e:
                task.set_result(e.value)
        
        # Phase 2: Poll I/O events (non-blocking)
        ready_io_events = poll_io_events(timeout=0)
        for fd, event in ready_io_events:
            if fd in io_waiting:
                task = io_waiting.pop(fd)
                ready_queue.append(task)
        
        # Phase 3: Check timers
        now = time.time()
        while timer_heap and timer_heap.peek_time() <= now:
            _, task = timer_heap.pop()
            ready_queue.append(task)
        
        # Phase 4: If nothing to do, wait for I/O or timer
        if not ready_queue:
            next_timer = timer_heap.peek_time() if timer_heap else None
            timeout = next_timer - now if next_timer else None
            ready_io_events = poll_io_events(timeout=timeout)
            # Process I/O events and continue loop
```

#### Coroutine State Machine

```python
async def example_coroutine():
    print("Step 1: Starting")           # State: RUNNING
    await asyncio.sleep(0.1)            # State: SUSPENDED (waiting for timer)
    print("Step 2: After sleep")       # State: RUNNING (resumed)
    result = await fetch_from_api()     # State: SUSPENDED (waiting for I/O)
    print(f"Step 3: Got {result}")     # State: RUNNING (resumed)
    return result                       # State: FINISHED

# State transitions:
# CREATED -> RUNNING -> SUSPENDED -> RUNNING -> SUSPENDED -> RUNNING -> FINISHED
#            ^                        ^                      ^
#            |                        |                      |
#         First call              Sleep complete        API response
```

#### Concurrency Through Cooperation

```python
async def task_a():
    print("Task A: Starting")
    await asyncio.sleep(1)      # Yields control for 1 second
    print("Task A: Continuing")
    await asyncio.sleep(1)      # Yields control for 1 second
    print("Task A: Finished")

async def task_b():
    print("Task B: Starting")
    await asyncio.sleep(0.5)    # Yields control for 0.5 seconds
    print("Task B: Continuing")
    await asyncio.sleep(1.5)    # Yields control for 1.5 seconds
    print("Task B: Finished")

# Timeline when run concurrently:
# t=0.0: Task A starts, Task B starts
# t=0.0: Both tasks yield (sleep)
# t=0.5: Task B resumes and continues
# t=0.5: Task B yields again (sleep 1.5s)
# t=1.0: Task A resumes and continues  
# t=1.0: Task A yields again (sleep 1s)
# t=2.0: Task A resumes and finishes, Task B resumes and finishes
```

### Event Loop Types and Implementations

#### Default Event Loop (SelectorEventLoop)

```python
import asyncio
import sys

# Different event loops for different platforms
if sys.platform == 'win32':
    # Windows: ProactorEventLoop (IOCP-based)
    loop = asyncio.ProactorEventLoop()
else:
    # Unix/Linux: SelectorEventLoop (epoll/kqueue-based)
    loop = asyncio.SelectorEventLoop()

# Key differences:
# - SelectorEventLoop: Uses select/poll/epoll for I/O multiplexing
# - ProactorEventLoop: Uses IOCP (I/O Completion Ports) on Windows
# - Both provide the same async/await interface
```

#### Custom Event Loop Integration

```python
# Example: Integration with Qt event loop
class QtAsyncioEventLoop:
    def __init__(self, qt_app):
        self.qt_app = qt_app
        self.asyncio_loop = asyncio.new_event_loop()
        
    def run_forever(self):
        # Integrate asyncio with Qt's event loop
        timer = QTimer()
        timer.timeout.connect(self._process_asyncio_events)
        timer.start(10)  # Check every 10ms
        
        self.qt_app.exec_()
    
    def _process_asyncio_events(self):
        # Process asyncio events within Qt's event loop
        self.asyncio_loop.call_soon(lambda: None)
        self.asyncio_loop._run_once()
```

### Event Loop Debugging and Introspection

#### Loop State Inspection

```python
import asyncio

async def inspect_loop():
    loop = asyncio.get_running_loop()
    
    print(f"Loop is running: {loop.is_running()}")
    print(f"Loop is closed: {loop.is_closed()}")
    print(f"Current time: {loop.time()}")
    
    # Get all tasks
    all_tasks = asyncio.all_tasks(loop)
    print(f"Active tasks: {len(all_tasks)}")
    
    for task in all_tasks:
        print(f"  Task: {task.get_name()}, State: {task._state}")
        if not task.done():
            print(f"    Stack: {task.get_stack()}")

# Enable debug mode for detailed diagnostics
asyncio.run(inspect_loop(), debug=True)
```

#### Performance Monitoring

```python
class EventLoopProfiler:
    def __init__(self):
        self.task_times = {}
        self.io_wait_times = {}
        
    def profile_task(self, task_name):
        def decorator(coro_func):
            async def wrapper(*args, **kwargs):
                start_time = asyncio.get_event_loop().time()
                try:
                    result = await coro_func(*args, **kwargs)
                    return result
                finally:
                    end_time = asyncio.get_event_loop().time()
                    self.task_times[task_name] = end_time - start_time
            return wrapper
        return decorator
    
    def report(self):
        print("Task Performance Report:")
        for task, duration in self.task_times.items():
            print(f"  {task}: {duration:.4f}s")

# Usage
profiler = EventLoopProfiler()

@profiler.profile_task("data_fetch")
async def fetch_data():
    await asyncio.sleep(0.1)
    return "data"
```

---

## The Testing Framework Problem

### Why unittest.TestCase Fails with Async Code

```python
class RegularTest(unittest.TestCase):
    def test_async_function(self):
        # This is synchronous context - no event loop running
        async def async_operation():
            await asyncio.sleep(0.1)
            return "result"
        
        # This returns a coroutine object, not the result
        coro = async_operation()
        print(type(coro))  # <class 'coroutine'>
        
        # Can't await here - no event loop!
        # result = await coro  # SyntaxError in sync function
        
        # This doesn't work either - needs event loop
        # result = coro.send(None)  # RuntimeError: no running event loop
        
        # Even manually running event loop is problematic
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(coro)  # Works but has issues
```

### The Fundamental Mismatch

| unittest.TestCase | Async Code |
|------------------|------------|
| Synchronous execution | Asynchronous execution |
| Direct function calls | Coroutines + event loop |
| Immediate results | Awaitable objects |
| No concurrency | Cooperative multitasking |
| Stack-based execution | Event-driven execution |

### Traditional "Solutions" and Their Problems

```python
# Approach 1: asyncio.run() - Creates new loop each time
def test_with_asyncio_run(self):
    async def async_test():
        result = await some_async_function()
        self.assertEqual(result, "expected")
    
    asyncio.run(async_test())
    # Problems:
    # - New event loop per test (expensive)
    # - Can't share resources between setUp/test/tearDown
    # - Difficult to mock async operations consistently

# Approach 2: get_event_loop().run_until_complete()
def test_with_run_until_complete(self):
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(some_async_function())
    # Problems:
    # - Manual event loop management
    # - Loop state leaks between tests
    # - Cleanup complexity

# Approach 3: Manual event loop management
def setUp(self):
    self.loop = asyncio.new_event_loop()
    asyncio.set_event_loop(self.loop)

def tearDown(self):
    self.loop.close()
    # Problems:
    # - Complex, error-prone
    # - Doesn't handle nesting
    # - Resource leak potential
    # - Async setUp/tearDown not supported
```

---

## IsolatedAsyncioWrapperTestCase: The Solution

### Architecture Overview

```python
# Provided by: Hummingbot (extends Python's unittest.IsolatedAsyncioTestCase)
class IsolatedAsyncioWrapperTestCase(unittest.IsolatedAsyncioTestCase):
    """
    Extends Python's IsolatedAsyncioTestCase with additional features:
    - Proper event loop lifecycle management
    - Async setUp/tearDown methods
    - Integration with Hummingbot's testing patterns
    - Enhanced resource cleanup
    """
```

### The Complete Lifecycle

```
┌─────────────────────────────────────────────────────────────────┐
│                    Test Class Lifecycle                         │
└─────────────────────────────────────────────────────────────────┘

1. setUpClass() [Class Level - Once]
   ├── Save current event loop state
   ├── Create main event loop for the class
   ├── Set up shared resources
   └── Initialize class-level mocks

2. setUp() [Per Test - Sync] 
   ├── Create isolated event loop for this test
   ├── Set event loop as current
   ├── Basic synchronous setup
   └── Prepare for async setup

3. asyncSetUp() [Per Test - Async]
   ├── Run in the isolated event loop
   ├── Set up patches and mocks
   ├── Initialize async resources
   ├── Configure test-specific behavior
   └── Prepare test environment

4. Test Method Execution
   ├── Async test methods wrapped automatically
   ├── run_until_complete() bridges sync/async
   ├── All async operations use isolated loop
   ├── Assertions and verifications
   └── Error handling and reporting

5. asyncTearDown() [Per Test - Async]
   ├── Clean up async resources
   ├── Stop patches and mocks
   ├── Close connections and streams
   ├── Cancel pending tasks
   └── Async cleanup operations

6. tearDown() [Per Test - Sync]
   ├── Final synchronous cleanup
   ├── Restore event loop state
   ├── Verify no resource leaks
   └── Reset test environment

7. tearDownClass() [Class Level - Once]
   ├── Restore original event loop
   ├── Clean up shared resources
   ├── Final state restoration
   └── Class-level cleanup
```

### Key Features

#### 1. Event Loop Isolation
```python
class TestExample(IsolatedAsyncioWrapperTestCase):
    async def test_method_1(self):
        # Gets its own dedicated event loop
        loop = asyncio.get_running_loop()
        loop_id_1 = id(loop)
        
    async def test_method_2(self):
        # Gets a completely different event loop
        loop = asyncio.get_running_loop()
        loop_id_2 = id(loop)
        # loop_id_1 != loop_id_2 - complete isolation
        # Even if test_method_1 had lingering tasks, they don't affect this test
```

#### 2. Automatic Async Wrapping
```python
# You write this:
async def test_async_operation(self):
    result = await some_async_function()
    self.assertEqual(result, "expected")

# Framework automatically converts to:
def test_async_operation(self):
    async def _async_test():
        result = await some_async_function()
        self.assertEqual(result, "expected")
    
    # Run in the isolated event loop
    self.loop.run_until_complete(_async_test())
```

#### 3. Proper Resource Management
```python
async def asyncSetUp(self):
    # All these resources are properly managed in the same event loop
    self.mock_connection = AsyncMock()
    self.patcher = patch('some.module.function')
    self.mock_func = self.patcher.start()
    
    # Async resources initialized in correct context
    self.websocket_client = await create_websocket_client()
    
async def asyncTearDown(self):
    # Cleanup happens in the same event loop context
    self.patcher.stop()
    
    # Async cleanup in proper context
    if hasattr(self.websocket_client, 'close'):
        await self.websocket_client.close()
    
    if hasattr(self.mock_connection, 'close'):
        await self.mock_connection.close()
```

---

## Mock vs Patch: The Core Concepts

### Understanding the Fundamental Difference

This is the most important concept to understand in testing. Let me explain the mental models:

#### Mock: Creating Fake Objects

**Mock** creates a **new fake object** that you control completely.

```python
from unittest.mock import Mock

# Think of Mock as "creating a robot actor"
# You build a robot that pretends to be something else
robot_database = Mock()

# You program the robot's responses
robot_database.get_user.return_value = {"name": "Alice", "id": 123}
robot_database.is_connected = True

# The robot performs as programmed
user = robot_database.get_user("alice")  # Returns {"name": "Alice", "id": 123}
print(robot_database.is_connected)      # Returns True

# You can verify what the robot was asked to do
robot_database.get_user.assert_called_with("alice")
```

**When to use Mock:**
- When you want to create a **new fake object from scratch**
- When you're testing code that **creates its own instances**
- When you want to **simulate objects that don't exist yet**

#### patch: Temporarily Replacing Real Objects

**patch** **replaces an existing real object** with a mock temporarily.

```python
from unittest.mock import patch

# Real module with real functions
# database_module.py
def connect_to_database():
    # This actually connects to a real database
    return RealDatabaseConnection()

def get_user(user_id):
    # This actually queries the database
    db = connect_to_database()
    return db.query(f"SELECT * FROM users WHERE id={user_id}")

# Testing code that uses the real module
# Think of patch as "body double substitution"
# Like replacing an actor with a stunt double during filming

@patch('database_module.connect_to_database')  # Replace the real function
def test_user_lookup(mock_connect):
    # Now when the code calls connect_to_database(), it gets the mock instead
    mock_db = Mock()
    mock_db.query.return_value = {"name": "Alice", "id": 123}
    mock_connect.return_value = mock_db
    
    # This calls the REAL get_user function, but get_user will use the MOCK database
    result = get_user(123)
    
    # Verify the real code called the mock correctly
    mock_connect.assert_called_once()
    mock_db.query.assert_called_with("SELECT * FROM users WHERE id=123")
```

**When to use patch:**
- When you want to **replace existing real objects/functions**
- When testing code that **imports and uses modules**
- When you need to **intercept calls** to external systems

### The Key Mental Models

```
Mock Mental Model: "Build a Robot Actor"
┌─────────────────────────────────────────────────────────────┐
│ You: "I need a fake database for my test"                  │
│ Mock: "Here's a robot that pretends to be a database!"     │
│ You: Configure robot's behavior                            │
│ Your Code: Uses the robot directly                         │
│ Robot: Performs as programmed and remembers what happened  │
└─────────────────────────────────────────────────────────────┘

patch Mental Model: "Stunt Double Substitution"
┌─────────────────────────────────────────────────────────────┐
│ You: "I need to replace the real database during testing"  │
│ patch: "I'll swap it with a stunt double!"                 │
│ Your Code: Calls what it thinks is the real database       │
│ patch: Redirects the call to the stunt double              │
│ Stunt Double: Performs as programmed                       │
│ patch: Puts the real database back when test ends          │
└─────────────────────────────────────────────────────────────┘
```

### Detailed Examples Showing the Difference

#### Example 1: Mock - Creating a Fake Object

```python
from unittest.mock import Mock

# Scenario: Testing a UserService that needs a database
class UserService:
    def __init__(self, database):
        self.database = database
    
    def create_user(self, name):
        user_id = self.database.insert_user(name)
        return f"Created user {name} with ID {user_id}"

# Test using Mock - create a fake database
def test_user_service_with_mock():
    # Create a robot that pretends to be a database
    fake_database = Mock()
    fake_database.insert_user.return_value = 42
    
    # Give the fake database to the service
    service = UserService(fake_database)
    
    # Test the service
    result = service.create_user("Alice")
    
    # Verify behavior
    assert result == "Created user Alice with ID 42"
    fake_database.insert_user.assert_called_with("Alice")
```

#### Example 2: patch - Replacing a Real Object

```python
from unittest.mock import patch

# Real module that our code imports
# database.py
def get_database_connection():
    # This would normally connect to a real database
    return RealDatabase()

# Code under test that imports the real module
# user_service.py
import database

class UserService:
    def create_user(self, name):
        # This calls the REAL get_database_connection function
        db = database.get_database_connection()
        user_id = db.insert_user(name)
        return f"Created user {name} with ID {user_id}"

# Test using patch - replace the real function
@patch('database.get_database_connection')  # Replace the real function
def test_user_service_with_patch(mock_get_connection):
    # Configure what the replacement should return
    fake_database = Mock()
    fake_database.insert_user.return_value = 42
    mock_get_connection.return_value = fake_database
    
    # Create service (it will use the replaced function)
    service = UserService()
    
    # Test the service - it thinks it's using the real database
    result = service.create_user("Alice")
    
    # Verify the replacement was called
    assert result == "Created user Alice with ID 42"
    mock_get_connection.assert_called_once()
    fake_database.insert_user.assert_called_with("Alice")
```

### When to Use Mock vs patch

```
Use Mock when:
├── Creating test objects from scratch
├── Your code creates instances itself
├── You need a fake object with specific behavior
├── Testing interfaces and protocols
└── Building test data structures

Use patch when:
├── Replacing imported modules/functions
├── Intercepting calls to external systems
├── Your code calls global functions
├── Mocking system calls (time.time, os.environ)
├── Replacing class methods temporarily
└── Preventing side effects (file I/O, network calls)
```

### Real-World QTX Example

```python
# Example: Testing QTX SHM Manager

# WRONG: Using Mock when you should use patch
def test_shm_manager_wrong():
    # This creates a NEW fake posix_ipc, but your code still uses the REAL one
    fake_posix_ipc = Mock()
    
    # Your code imports and uses the REAL posix_ipc, not your fake one!
    manager = QtxShmManager("/test")  # Still uses real posix_ipc
    # This test will try to connect to actual shared memory!

# RIGHT: Using patch to replace the real posix_ipc
@patch('qtx_perpetual_shm_manager.posix_ipc')  # Replace the real import
def test_shm_manager_correct(mock_posix_ipc):
    # Configure the replacement
    mock_shm = Mock()
    mock_posix_ipc.SharedMemory.return_value = mock_shm
    
    # Now when QtxShmManager imports posix_ipc, it gets the mock
    manager = QtxShmManager("/test")  # Uses the mock posix_ipc
    # Safe to test without real shared memory
```

---

## Comprehensive Testing Tools Reference

### Core Mocking Tools (unittest.mock module)

#### 1. Mock - Basic Function/Object Mocking
**Purpose**: Create fake objects with controllable behavior
**Provided by**: Python standard library (`unittest.mock`)

```python
from unittest.mock import Mock

# Basic function mocking
mock_function = Mock(return_value="mocked_result")
assert mock_function() == "mocked_result"
mock_function.assert_called_once()

# With side effects (different return values for each call)
mock_function = Mock(side_effect=[1, 2, 3])
assert mock_function() == 1
assert mock_function() == 2
assert mock_function() == 3

# Object attribute mocking
mock_obj = Mock()
mock_obj.attribute = "value"
mock_obj.method.return_value = "method_result"
```

#### 2. MagicMock - Magic Method Support
**Purpose**: Mock objects that need to support Python magic methods (`__len__`, `__str__`, etc.)
**Provided by**: Python standard library (`unittest.mock`)

```python
from unittest.mock import MagicMock

# Supports magic methods automatically
mock_obj = MagicMock()
mock_obj.__len__.return_value = 5
mock_obj.__str__.return_value = "mocked object"
mock_obj.__getitem__.return_value = "item"

assert len(mock_obj) == 5
assert str(mock_obj) == "mocked object"
assert mock_obj[0] == "item"

# Iterator support
mock_obj.__iter__.return_value = iter([1, 2, 3])
assert list(mock_obj) == [1, 2, 3]
```

#### 3. AsyncMock - Async Function Mocking
**Purpose**: Mock async functions and coroutines
**Provided by**: Python standard library (`unittest.mock`, Python 3.8+)

```python
from unittest.mock import AsyncMock

# For async functions
async_mock = AsyncMock(return_value="async_result")

async def test_async_function():
    result = await async_mock()
    assert result == "async_result"
    async_mock.assert_awaited_once()

# With async side effects
async def async_side_effect():
    await asyncio.sleep(0.01)
    return "side_effect_result"

async_mock = AsyncMock(side_effect=async_side_effect)
```

### Advanced Patching Tools

#### 4. patch() - Context Manager/Decorator Patching
**Purpose**: Temporarily replace real objects/functions with mocks
**Provided by**: Python standard library (`unittest.mock`)

```python
from unittest.mock import patch

# As decorator - mock is passed as parameter
@patch('module.function_name')
def test_function(self, mock_function):
    mock_function.return_value = "mocked"
    result = module.function_name()
    assert result == "mocked"

# As context manager - mock returned by 'as'
def test_function(self):
    with patch('module.function_name') as mock_function:
        mock_function.return_value = "mocked"
        result = module.function_name()
        assert result == "mocked"

# Manual start/stop - for setUp/tearDown
def asyncSetUp(self):
    self.patcher = patch('module.function_name')
    self.mock_function = self.patcher.start()

def asyncTearDown(self):
    self.patcher.stop()
```

#### 5. patch.object() - Object Method Patching
**Purpose**: Replace specific methods on specific objects or classes
**Provided by**: Python standard library (`unittest.mock`)

```python
from unittest.mock import patch

class MyClass:
    def method(self):
        return "original"

obj = MyClass()

# Patch specific object instance method
with patch.object(obj, 'method', return_value="mocked"):
    assert obj.method() == "mocked"

# Patch class method for all instances
with patch.object(MyClass, 'method', return_value="mocked"):
    new_obj = MyClass()
    assert new_obj.method() == "mocked"
```

### Tool Selection Decision Tree

```
What do you need to do?

├── Create a fake object for testing?
│   ├── Need magic methods (__len__, __str__, etc.)?
│   │   └── Use MagicMock
│   ├── Need async support?
│   │   └── Use AsyncMock
│   └── Basic fake object?
│       └── Use Mock
│
├── Replace an existing real object/function?
│   ├── Replace one thing?
│   │   ├── Global function/module?
│   │   │   └── Use patch()
│   │   └── Object method?
│   │       └── Use patch.object()
│   ├── Replace multiple things?
│   │   └── Use patch.multiple()
│   └── Replace dictionary values?
│       └── Use patch.dict()
│
└── Need interface validation?
    └── Use create_autospec() or spec parameter
```

---

## Complete Testing Flow

### Example Test Implementation

```python
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import patch, AsyncMock, MagicMock, call, ANY

class TestQtxShmManager(IsolatedAsyncioWrapperTestCase):
    @classmethod  
    def setUpClass(cls):
        """Class-level setup - runs once"""
        super().setUpClass()
        cls.test_config = {
            "api_key": "test_key",
            "api_secret": "test_secret", 
            "shm_name": "/test_segment"
        }
    
    async def asyncSetUp(self):
        """Per-test async setup"""
        await super().asyncSetUp()
        
        # 1. Set up patches for external dependencies
        # Replace the real posix_ipc module with a mock
        self.posix_ipc_patcher = patch('hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager.posix_ipc')
        self.mock_posix_ipc = self.posix_ipc_patcher.start()
        
        # Replace the real mmap module with a mock
        self.mmap_patcher = patch('hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager.mmap')
        self.mock_mmap = self.mmap_patcher.start()
        
        # Replace the real time module with a mock
        self.time_patcher = patch('hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager.time')
        self.mock_time = self.time_patcher.start()
        
        # 2. Configure mock behavior
        # Create a fake shared memory object
        self.mock_shm = MagicMock()
        self.mock_posix_ipc.SharedMemory.return_value = self.mock_shm
        
        # 3. Set up exception hierarchy  
        self.mock_posix_ipc.Error = type('Error', (Exception,), {})
        self.mock_posix_ipc.ExistentialError = type('ExistentialError', (self.mock_posix_ipc.Error,), {})
        
        # 4. Configure time behavior
        self.mock_time.time.return_value = 1631234567.123456
        
        # 5. Create system under test
        from hummingbot.connector.derivative.qtx_perpetual.qtx_perpetual_shm_manager import QtxShmManager
        self.manager = QtxShmManager(**self.test_config)
    
    async def test_connection_success(self):
        """Test successful connection"""
        # Configure mocks for success path
        self.mock_shm.fd = 3
        self.mock_mmap.mmap.return_value = b'\x00' * 4096
        
        # Execute test
        result = await self.manager.connect()
        
        # Verify behavior
        self.assertTrue(result)
        self.mock_posix_ipc.SharedMemory.assert_called_once_with("/test_segment")
        self.mock_mmap.mmap.assert_called_once_with(3, 4096)
        
    async def test_connection_failure(self):
        """Test connection failure handling"""
        # Configure mock to raise exception
        self.mock_posix_ipc.SharedMemory.side_effect = self.mock_posix_ipc.ExistentialError("Shared memory not found")
        
        # Test exception handling
        with self.assertRaises(ConnectionError) as context:
            await self.manager.connect()
        
        self.assertIn("Shared memory not found", str(context.exception))
    
    async def asyncTearDown(self):
        """Per-test async cleanup"""
        # Stop all patches
        self.time_patcher.stop()
        self.mmap_patcher.stop() 
        self.posix_ipc_patcher.stop()
        
        # Clean up manager resources
        if hasattr(self.manager, 'disconnect'):
            await self.manager.disconnect()
            
        await super().asyncTearDown()
```

---

## Best Practices and Common Pitfalls

### ✅ Best Practices

#### 1. Use Appropriate Mock Types
```python
# ✅ Good - Use AsyncMock for async functions
self.mock_async_func = AsyncMock(return_value="result")
await self.mock_async_func()  # Returns "result"

# ❌ Bad - Regular Mock won't work properly with await
self.mock_async_func = Mock(return_value="result")
# await self.mock_async_func()  # Returns "result", not awaitable!
```

#### 2. Choose Mock vs patch Correctly
```python
# ✅ Good - Use patch to replace real imports
@patch('my_module.external_api')
def test_api_call(self, mock_api):
    mock_api.get_data.return_value = {"status": "success"}
    # Test code that imports and uses external_api

# ❌ Bad - Using Mock when you need patch
def test_api_call(self):
    fake_api = Mock()  # This doesn't replace the real import!
    # Your code still uses the real external_api
```

#### 3. Proper Resource Management
```python
# ✅ Good - Always stop patches in tearDown
async def asyncSetUp(self):
    self.patchers = []
    
    self.patcher1 = patch('module.function1')
    self.mock_func1 = self.patcher1.start()
    self.patchers.append(self.patcher1)

async def asyncTearDown(self):
    # Stop in reverse order
    for patcher in reversed(self.patchers):
        patcher.stop()
```

### ❌ Common Pitfalls

#### 1. Mock vs patch Confusion
```python
# ❌ Wrong - Using Mock when you need patch
def test_database_connection(self):
    # This creates a fake database but doesn't replace the real one
    fake_db = Mock()
    # Your code still tries to connect to the real database!

# ✅ Right - Using patch to replace the real database
@patch('database_module.connect')
def test_database_connection(self, mock_connect):
    # Now when code calls database_module.connect(), it gets the mock
    mock_connect.return_value = fake_connection
```

#### 2. Event Loop Confusion
```python
# ❌ Wrong - Mixing event loops
async def test_mixed_loops(self):
    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()
    
    # This creates confusion and potential deadlocks
    task1 = loop1.create_task(some_coro())
    task2 = loop2.create_task(other_coro())
    
    # Can't await tasks from different loops!

# ✅ Right - Use the test's event loop
async def test_proper_loop(self):
    # Uses the isolated test event loop automatically
    result1 = await some_coro()
    result2 = await other_coro()
    
    # Or explicitly use current loop
    loop = asyncio.get_running_loop()
    task1 = loop.create_task(some_coro())
    task2 = loop.create_task(other_coro())
    results = await asyncio.gather(task1, task2)
```

---

## Summary

The key insights for async testing are:

### 1. Event Loop Understanding
- **Event loops** are the foundation of async programming
- They provide **cooperative multitasking** through **single-threaded execution**
- **Tasks yield control** at `await` points, allowing other tasks to run
- **Proper event loop management** is crucial for test isolation

### 2. Mock vs patch Distinction
- **Mock**: Creates a new fake object that you control ("Build a robot actor")
- **patch**: Replaces an existing real object temporarily ("Stunt double substitution")
- **Use Mock when:** Creating test objects from scratch
- **Use patch when:** Replacing real objects that your code imports/uses

### 3. Testing Framework Integration
- **IsolatedAsyncioWrapperTestCase** provides proper async testing infrastructure
- **Each test gets isolated event loops** preventing cross-contamination
- **Automatic async wrapping** makes async test methods work seamlessly
- **Proper resource management** ensures clean test environments

This understanding enables comprehensive testing of complex async systems like the QTX perpetual connector while maintaining code quality and test reliability.