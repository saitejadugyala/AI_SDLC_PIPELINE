const fs = require('fs');
const path = require('path');

const DB_FILE = path.join(__dirname, 'tasks.json');

/**
 * Initialize the database file if it does not exist.
 * Creates an empty array in JSON format.
 */
function initializeDatabase() {
  if (!fs.existsSync(DB_FILE)) {
    fs.writeFileSync(DB_FILE, '[]', { encoding: 'utf8' });
  }
}

/**
 * Fetch all tasks from the database.
 * @returns {Array} List of task objects.
 */
function fetchTasks() {
  try {
    const data = fs.readFileSync(DB_FILE, { encoding: 'utf8' });
    return JSON.parse(data);
  } catch (err) {
    // If error, return empty array
    return [];
  }
}

/**
 * Insert a new task into the database.
 * @param {Object} task Task object with at least { id, title, completed } fields.
 */
function insertTask(task) {
  const tasks = fetchTasks();
  tasks.push(task);
  fs.writeFileSync(DB_FILE, JSON.stringify(tasks, null, 2), { encoding: 'utf8' });
}

/**
 * Update an existing task in the database.
 * Matches task by id (number or string, normalized to string).
 * @param {Object} task Task object with updated fields.
 * @returns {boolean} True if update successful, false if task not found.
 */
function updateTaskRecord(task) {
  const tasks = fetchTasks();
  const taskIdStr = String(task.id);
  const index = tasks.findIndex(t => String(t.id) === taskIdStr);
  if (index === -1) {
    return false;
  }
  tasks[index] = { ...tasks[index], ...task };
  fs.writeFileSync(DB_FILE, JSON.stringify(tasks, null, 2), { encoding: 'utf8' });
  return true;
}

/**
 * Delete a task from the database by id.
 * @param {string|number} taskId The id of the task to remove.
 * @returns {boolean} True if delete successful, false if not found.
 */
function deleteTaskRecord(taskId) {
  const tasks = fetchTasks();
  const taskIdStr = String(taskId);
  const index = tasks.findIndex(t => String(t.id) === taskIdStr);
  if (index === -1) {
    return false;
  }
  tasks.splice(index, 1);
  fs.writeFileSync(DB_FILE, JSON.stringify(tasks, null, 2), { encoding: 'utf8' });
  return true;
}

module.exports = {
  initializeDatabase,
  insertTask,
  fetchTasks,
  updateTaskRecord,
  deleteTaskRecord
};
